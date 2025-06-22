import os
import requests
import hashlib
import json
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from tqdm import tqdm
import time

# --- CONFIGURATION ---
BASE_URL = "https://docs.espressif.com/projects/esp-idf/en/latest/"
HASH_FILE = "page_hashes.json"
SOURCE_DIR = "source_html"
UPDATE_LOG = "updated_pages.log"

# --- FUNCTIONS ---

def load_hashes():
    """Loads the previously saved page hashes from the JSON file."""
    if not os.path.exists(HASH_FILE):
        return {}
    try:
        with open(HASH_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}

def save_hashes(hashes):
    """Saves the updated hashes to the JSON file."""
    with open(HASH_FILE, 'w', encoding='utf-8') as f:
        json.dump(hashes, f, indent=4)

def get_page_content_and_hash(session, url):
    """Gets the content of a page and returns its SHA256 hash."""
    try:
        response = session.get(url, timeout=10)
        response.raise_for_status() # Raise an exception for bad status codes
        content = response.content
        return content, hashlib.sha256(content).hexdigest()
    except requests.RequestException as e:
        print(f"\nError fetching {url}: {e}")
        return None, None

def discover_links(session, url, base_url):
    """Discovers all internal links on a given page."""
    links = set()
    try:
        response = session.get(url, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            # Join the URL to handle relative paths
            full_url = urljoin(base_url, href)
            # Clean off fragment identifiers and query parameters
            full_url = urlparse(full_url)._replace(fragment="", query="").geturl()
            # Check if the link is internal to the documentation site
            if full_url.startswith(base_url):
                links.add(full_url)
    except requests.RequestException as e:
        print(f"\nCould not discover links on {url}: {e}")
    return links

def save_html_file(content, url, base_dir, base_url):
    """Saves the HTML content to a local file, preserving directory structure."""
    if not content:
        return

    # Create a path that mirrors the URL structure
    parsed_url = urlparse(url)
    relative_path = parsed_url.path.lstrip('/')
    # Replace the base part of the path if it exists
    base_path_part = urlparse(base_url).path.lstrip('/')
    if relative_path.startswith(base_path_part):
        relative_path = relative_path[len(base_path_part):].lstrip('/')

    # If the path ends in a directory, assume index.html
    if relative_path == '' or relative_path.endswith('/'):
        file_name = 'index.html'
    else:
        file_name = os.path.basename(relative_path)
        if '.' not in file_name:
             file_name += '.html'

    dir_path = os.path.join(base_dir, os.path.dirname(relative_path))
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, file_name)

    with open(file_path, 'wb') as f:
        f.write(content)

# --- MAIN SCRIPT ---

def main():
    """Main function to monitor and update documentation."""
    print("Starting documentation monitor...")
    
    # Use a session for connection pooling
    session = requests.Session()
    
    # 1. Discover all links recursively
    print(f"Discovering all pages starting from {BASE_URL}...")
    all_links = set()
    to_crawl = {BASE_URL}
    crawled = set()

    pbar_discover = tqdm(desc="Discovering pages", unit=" pages")
    while to_crawl:
        current_url = to_crawl.pop()
        if current_url in crawled:
            continue
        
        new_links = discover_links(session, current_url, BASE_URL)
        to_crawl.update(new_links - crawled)
        all_links.update(new_links)
        crawled.add(current_url)
        pbar_discover.update(1)
        pbar_discover.set_postfix_str(f"Found {len(all_links)} unique URLs")

    pbar_discover.close()
    print(f"Discovery complete. Found {len(all_links)} unique pages.")

    # 2. Check for updates
    print("\nChecking for updated or new pages...")
    previous_hashes = load_hashes()
    current_hashes = {}
    updated_or_new_urls = []

    with open(UPDATE_LOG, 'w', encoding='utf-8') as log_file:
        for url in tqdm(sorted(list(all_links)), desc="Checking pages", unit=" page"):
            content, new_hash = get_page_content_and_hash(session, url)
            if not new_hash:
                continue

            current_hashes[url] = new_hash
            if previous_hashes.get(url) != new_hash:
                updated_or_new_urls.append(url)
                log_file.write(url + '\n')
                # Save the updated file
                save_html_file(content, url, SOURCE_DIR, BASE_URL)
            
            # Small delay to be polite to the server
            time.sleep(0.1)

    # 3. Report and save
    if not updated_or_new_urls:
        print("\nNo changes detected. Your local documentation is up to date.")
    else:
        print(f"\nFound {len(updated_or_new_urls)} new or updated pages.")
        print(f"- The list of these pages has been saved to {UPDATE_LOG}")
        print(f"- The new/updated files have been downloaded to the '{SOURCE_DIR}' directory.")
        print("\nNext step: Run the translation script to translate these files.")

    save_hashes(current_hashes)
    print(f"Updated page hashes have been saved to {HASH_FILE}.")

if __name__ == "__main__":
    main()
