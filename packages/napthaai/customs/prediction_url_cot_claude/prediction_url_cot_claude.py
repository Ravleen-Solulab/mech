from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from docstring_parser import parse
from googleapiclient.discovery import build
from itertools import islice
import json
import re
from io import BytesIO
import PyPDF2
import anthropic
from pydantic import BaseModel, Field
from readability import Document as ReadabilityDocument
import requests
from requests.exceptions import RequestException, TooManyRedirects
from markdownify import markdownify as md
from typing import Any, Dict, Generator, List, Optional, Tuple, Callable
from tiktoken import encoding_for_model


DEFAULT_CLAUDE_SETTINGS = {
    "max_tokens": 1000,
    "temperature": 0,
}
MAX_TOKENS = {
    'claude-2': 200_0000,
    'claude-2.1': 200_0000,
    'claude-3-haiku-20240307': 200_0000,
    'claude-3-sonnet-20240229': 200_0000,
    'claude-3-opus-20240229': 200_0000,
}
ALLOWED_TOOLS = [
    "prediction-url-cot-claude",
]
ALLOWED_MODELS = [
    "claude-3-haiku-20240307",
]
TOOL_TO_ENGINE = {tool: "claude-3-haiku-20240307" for tool in ALLOWED_TOOLS}
NUM_QUERIES = 5
NUM_URLS_PER_QUERY = 3
HTTP_TIMEOUT = 20
HTTP_MAX_REDIRECTS = 5
HTTP_MAX_RETIES = 2
MIN_WORDS = 100
MAX_DOC_WORDS = 10000
N_DOCS = 5

PREDICTION_PROMPT = """
Here is some additional background information that may be relevant to the question:
<additional_information> {ADDITIONAL_INFORMATION} </additional_information>

A user has asked the following:

<user_prompt> {USER_PROMPT} </user_prompt>

Carefully consider the user's question and the additional information provided. Think through the likelihood of the event the user asked about actually happening in the future, based on the details given. Write out your reasoning and analysis in a section.

Now, based on your analysis above, provide a prediction of the probability the event will happen, as p_yes between 0 and 1. Also provide the probability it will not happen, as p_no between 0 and 1. The two probabilities should sum to 1.

p_yes: p_no:

How useful was the additional information in allowing you to make a prediction? Provide your rating as info_utility, a number between 0 and 1.

info_utility:

Finally, considering everything, what is your overall confidence in your prediction? Provide your confidence as a number between 0 and 1.

confidence:

Make sure the values you provide are between 0 and 1. And p_yes and p_no should sum to 1.

Your response should be structured as follows:
<p_yes></p_yes>
<p_no></p_no>
<info_utility></info_utility>
<confidence></confidence>
<analysis></analysis>
"""


URL_QUERY_PROMPT = """
Here is the user prompt: {USER_PROMPT}

Please read the prompt carefully and identify the key pieces of information that need to be searched for in order to comprehensively address the topic.

Brainstorm a list of {NUM_QUERIES} different search queries that cover various aspects of the user prompt. Each query should be focused on a specific sub-topic or question related to the overarching prompt.

Please write each search query inside its own tags, like this: example search query here

The queries should be concise while still containing enough information to return relevant search results. Focus the queries on gathering factual information to address the prompt rather than opinions.

After you have written all {NUM_QUERIES} search queries, please submit your final response.

<queries></queries>
"""

SYSTEM_PROMPT = """You are a world class algorithm for generating structured output from a given input."""


class Document(BaseModel):
    text: str
    url: str
    embedding: Optional[List[float]] = None


def count_tokens(text: str, model: str) -> int:
    """Count the number of tokens in a text."""
    enc = encoding_for_model(model)
    return len(enc.encode(text))


def multi_queries(
    client: anthropic.Anthropic,
    prompt: str,
    engine: str,
    num_queries: int,
    counter_callback: Optional[Callable[[int, int, str], None]] = None,
    temperature: Optional[float] = DEFAULT_CLAUDE_SETTINGS["temperature"],
    max_tokens: Optional[int] = DEFAULT_CLAUDE_SETTINGS["max_tokens"],
) -> List[str]:
    """Generate multiple queries for fetching information from the web."""
    url_query_prompt = URL_QUERY_PROMPT.format(
        USER_PROMPT=prompt, NUM_QUERIES=num_queries
    )

    messages = [
        {"role": "user", "content": url_query_prompt},
    ]

    response = client.messages.create(
        model=engine,
        messages=messages,
        system=SYSTEM_PROMPT,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if counter_callback:
        counter_callback(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=engine,
            token_counter=count_tokens,
        )
    queries = parser_query_response(response.content[0].text, num_queries=num_queries)
    queries.append(prompt)

    return queries, counter_callback


def parser_query_response(response: str, num_queries: int = 5) -> List[str]:
    """Parse the response from the query generation model with optional enhancements."""
    queries = response.split("<queries>")[1].split("</queries>")[0].split("\n")
    parsed_queries = [query.strip() for query in queries if query.strip()]
    enhanced_queries = []

    for query in parsed_queries:
        if query[0].isdigit():
            query = ". ".join(query.split(". ")[1:])
        query = query.replace('"', '')
        enhanced_queries.append(query)

    if len(enhanced_queries) == num_queries * 2:
        enhanced_queries = enhanced_queries[::2]

    # Remove doubel quotes from the queries
    final_queries = [query.replace('"', '') for query in enhanced_queries]

    # if there are any xml tags in the queries, remove them
    final_queries = [re.sub(r'<[^>]*>', '', query) for query in final_queries]
    
    return final_queries


def search_google(
    query: str, 
    api_key: str, 
    engine: str, 
    num: int
) -> List[str]:
    """Search Google for the given query."""
    service = build("customsearch", "v1", developerKey=api_key)
    search = (
        service.cse()
        .list(
            q=query,
            cx=engine,
            num=num,
        )
        .execute()
    )
    return [result["link"] for result in search["items"]]


def get_urls_from_queries(
    queries: List[str], api_key: str, engine: str, num: int
) -> List[str]:
    """Get URLs from search engine queries"""
    results = []
    for query in queries:
        try:
            for url in search_google(
                query=query,
                api_key=api_key,
                engine=engine,
                num=num,
            ):
                results.append(url)
        except Exception:
            pass
    unique_results = list(set(results))
    return unique_results


def extract_text(
    html: str,
    num_words: Optional[int] = None,
) -> str:
    """Extract text from a single HTML document"""
    text = ReadabilityDocument(html).summary()

    # use html2text to convert HTML to markdown
    text = md(text, heading_style="ATX")

    if text is None:
        return None

    if num_words:
        text = " ".join(text.split()[:num_words])
    else:
        text = " ".join(text.split())

    doc = Document(text=text, url="")
    return doc


def extract_text_from_pdf(url: str, num_words: Optional[int] = None) -> str:
    """Extract text from a PDF document at the given URL."""
    try:
        response = requests.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()

        if "application/pdf" not in response.headers.get("Content-Type", ""):
            return ValueError("URL does not point to a PDF document")

        with BytesIO(response.content) as pdf_file:
            reader = PyPDF2.PdfReader(pdf_file)
            text = ""
            for page in reader.pages:
                text += page.extract_text()

        doc = Document(text=text[:num_words] if num_words else text, date="", url=url)
        print(f"Using PDF: {url}: {doc.text[:300]}...")
        return doc
    
    except Exception as e:
        print(f"An error occurred: {e}")
        return None
    

def process_in_batches(
    urls: List[str], 
    window: int = 5, 
    timeout: int = HTTP_TIMEOUT,
    max_redirects: int = HTTP_MAX_REDIRECTS,
    retries: int = HTTP_MAX_RETIES,
) -> Generator[None, None, List[Tuple[Optional[Future], str]]]:
    """Iter URLs in batches with improved error handling and retry mechanism."""
    with ThreadPoolExecutor() as executor, requests.Session() as session:
        session.max_redirects = max_redirects
        for i in range(0, len(urls), window):
            batch = urls[i : i + window]
            futures = []
            for url in batch:
                future = None
                attempt = 0
                while attempt < retries:
                    try:
                        future = executor.submit(session.get, url, timeout=timeout)
                        break  
                    except (TooManyRedirects, RequestException) as e:
                        print(f"Attempt {attempt + 1} failed for {url}: {e}")
                        attempt += 1
                        if attempt == retries:
                            print(f"Max retries reached for {url}. Moving to next URL.")
                futures.append((future, url))
            yield futures


def extract_texts(urls: List[str], num_words: Optional[int] = None) -> List[Document]:
    """Extract texts from URLs with improved error handling, excluding failed URLs."""
    extracted_texts = []
    for batch in process_in_batches(urls=urls):
        for future, url in batch:
            if future is None:
                continue
            try:
                result = future.result()
                if result.status_code == 200:
                    # Check if URL ends with .pdf or content starts with %PDF
                    if url.endswith('.pdf') or result.content[:4] == b'%PDF':
                        doc = extract_text_from_pdf(url, num_words=num_words)
                    else:
                        doc = extract_text(html=result.text, num_words=num_words)
                    doc.url = url
                    extracted_texts.append(doc)
            except Exception as e:
                print(f"Error processing {url}: {e}")
                continue
    return extracted_texts


def extract_question(prompt: str) -> str:
    pattern = r'\"(.*?)\"'
    try:
        question = re.findall(pattern, prompt)[0]
    except Exception as e:
        question = prompt

    return question

def count_words(text: str) -> int:
    """Count the number of words in a text."""
    return len(text.split())

def select_docs(
    docs: List[Document], 
    n_docs: int,
    min_words: int = MIN_WORDS,
    max_words: int = MAX_DOC_WORDS,
) -> List[Document]:
    """Select N documents from the list."""

    word_counts = {doc.url: count_words(doc.text) for doc in docs}

    # Sort the documents by word count
    sorted_docs = sorted(word_counts, key=word_counts.get, reverse=True)

    selected_urls = []
    for u in sorted_docs:
        if word_counts[u] >= 60:
            selected_urls.append(u)
        if len(selected_urls) == 4:
            break

    # selected urls to doc
    selected_docs = [doc for doc in docs if doc.url in selected_urls]

    # make sure the selected documents are less than 10000 words
    for doc in selected_docs:
        if word_counts[doc.url] > 10000:
            doc.text = " ".join(doc.text.split()[:10000])

    return selected_docs

def fetch_additional_information(
    client: anthropic.Anthropic,
    prompt: str,
    engine: str,
    google_api_key: Optional[str],
    google_engine_id: Optional[str],
    counter_callback: Optional[Callable[[int, int, str], None]] = None,
    source_links: Optional[List[str]] = None,
    num_urls: Optional[int] = NUM_URLS_PER_QUERY,
    num_queries: Optional[int] = NUM_QUERIES,
    temperature: Optional[float] = DEFAULT_CLAUDE_SETTINGS["temperature"],
    max_tokens: Optional[int] = DEFAULT_CLAUDE_SETTINGS["max_tokens"],
    n_docs: int = N_DOCS,
) -> Tuple[str, Callable[[int, int, str], None]]:
    """Fetch additional information from the web."""

    # generate multiple queries for fetching information from the web
    try:
        queries, counter_callback = multi_queries(
            client=client,
            prompt=prompt,
            engine=engine,
            num_queries=num_queries,
            counter_callback=counter_callback,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        print(f"Queries: {queries}")
    except Exception as e:
        print(f"Error generating queries: {e}")
        queries = [prompt]

    # get the top URLs for the queries
    if not source_links:
        urls = get_urls_from_queries(
            queries=queries,
            api_key=google_api_key,
            engine=google_engine_id,
            num=NUM_URLS_PER_QUERY,
        )
        print(f"URLs: {urls}")

        urls = list(set(urls))

        # Extract text and dates from the URLs
        docs = extract_texts(
            urls=urls,
        )
    else:
        docs = []
        for url, content in islice(source_links.items(), num_urls or len(source_links)):
            doc = extract_text(html=content)
            doc.url = url
            docs.append(doc)

    # Remove None values from the list
    docs = [doc for doc in docs if doc]

    # remove empty documents ""
    filtered_docs = [doc for doc in docs if hasattr(doc, 'text') and doc.text != ""]

    # Select N urls to be used
    selected_docs = select_docs(filtered_docs, n_docs)

    # Format the additional information
    additional_information = "\n".join(
        [
            f"ARTICLE {i}, URL: {doc.url}, CONTENT: {doc.text}\n"
            for i, doc in enumerate(selected_docs)
        ]
    )

    return additional_information, counter_callback


def parser_prediction_response(response: str) -> str:
    """Parse the response from the prediction model."""
    results = {}
    for key in ["p_yes", "p_no", "info_utility", "confidence"]:
        try:
            value = response.split(f"<{key}>")[1].split(f"</{key}>")[0].strip()
            if key in ["p_yes", "p_no", "info_utility", "confidence"]:
                value = float(value)
            results[key] = value
        except Exception:
            raise ValueError(f"Error parsing {key}")

    results = json.dumps(results)
    return results


def run(**kwargs) -> Tuple[Optional[str], Any, Optional[Dict[str, Any]], Any]:
    """Run the task"""

    tool = kwargs["tool"]
    model = kwargs.get("model", TOOL_TO_ENGINE[tool])
    prompt = extract_question(kwargs["prompt"])
    max_tokens = kwargs.get("max_tokens", DEFAULT_CLAUDE_SETTINGS["max_tokens"])
    temperature = kwargs.get("temperature", DEFAULT_CLAUDE_SETTINGS["temperature"])
    num_urls = kwargs.get("num_urls", NUM_URLS_PER_QUERY)
    num_queries = kwargs.get("num_queries", NUM_QUERIES)
    n_docs = kwargs.get("n_docs", N_DOCS)
    counter_callback = kwargs.get("counter_callback", None)
    api_keys = kwargs.get("api_keys", {})
    google_api_key = api_keys.get("google_api_key", None)
    google_engine_id = api_keys.get("google_engine_id", None)
    client = anthropic.Anthropic(api_key=api_keys["anthropic"])

    # Make sure the model is supported
    if model not in ALLOWED_MODELS:
        raise ValueError(f"Model {model} not supported.")
    
    # make sure the tool is supported
    if tool not in ALLOWED_TOOLS:
        raise ValueError(f"Tool {tool} not supported.")
    
    engine = kwargs.get("model", TOOL_TO_ENGINE[tool])
    print(f"ENGINE: {engine}")
    
    additional_information, counter_callback = fetch_additional_information(
        client=client,
        prompt=prompt,
        engine=engine,
        google_api_key=google_api_key,
        google_engine_id=google_engine_id,
        counter_callback=counter_callback,
        source_links=kwargs.get("source_links", None),
        num_urls=num_urls,
        num_queries=num_queries,
        temperature=temperature,
        max_tokens=max_tokens,
        n_docs=n_docs,
    )

    # Generate the prediction prompt
    prediction_prompt = PREDICTION_PROMPT.format(
        ADDITIONAL_INFORMATION=additional_information,
        USER_PROMPT=prompt,
    )

    # Generate the prediction
    messages = [
        {"role": "user", "content": prediction_prompt},
    ]

    response = client.messages.create(
        model=engine,
        messages=messages,
        system=SYSTEM_PROMPT,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    if counter_callback:
        counter_callback(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=engine,
            token_counter=count_tokens,
        )   

    results = parser_prediction_response(response.content[0].text)
    
    return results, prediction_prompt, None, counter_callback
