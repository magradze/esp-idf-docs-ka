import os
import json
import logging
import re
from bs4 import BeautifulSoup, NavigableString
from tqdm import tqdm
import time
from google.cloud import translate_v2 as translate
from google.api_core import exceptions as google_exceptions

# --- CONFIGURATION ---
# -----------------------------------------------------------------------------
# GOOGLE CLOUD AUTHENTICATION:
# Make sure you have authenticated with Google Cloud.
# Set the GOOGLE_APPLICATION_CREDENTIALS environment variable to the path
# of your JSON key file.
# Example: export GOOGLE_APPLICATION_CREDENTIALS="/path/to/your/keyfile.json"
# -----------------------------------------------------------------------------

SOURCE_DIR = "source_html"
TRANSLATED_DIR = "translated_html"
TERMINOLOGY_FILE = "terminology.json"
LOG_FILE = "errors.log"
STATE_FILE = "translation_state.json" # Stores progress
TARGET_LANG = "ka"  # Target language for Google Translate (ka for Georgian)
# Set a very high limit, as we are now using the free credits.
# This will be tracked in the script but will not stop the translation.
CHAR_LIMIT_PER_MONTH = 999999999

# Tags to be translated
TRANSLATABLE_TAGS = ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'a', 'span', 'strong', 'em', 'td', 'th', 'figcaption', 'blockquote', 'title']
# Tags to be excluded from translation
EXCLUDED_TAGS = ['code', 'pre', 'script', 'style', 'kbd']

# --- LOGGING SETUP ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)

# --- FUNCTIONS ---

def load_terminology(file_path):
    """Loads the terminology dictionary from a JSON file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            terminology_data = json.load(f)
            return terminology_data.get("en_to_ka", {})
    except FileNotFoundError:
        print(f"Warning: Terminology file not found at '{file_path}'. Continuing without it.")
        return {}
    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from '{file_path}'. Check the file format.")
        logging.error(f"JSON decode error in {file_path}")
        return {}

def load_processed_files(state_file):
    """Loads the set of already processed file paths."""
    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_processed_files(state_file, processed_files):
    """Saves the set of processed file paths."""
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump(list(processed_files), f, indent=4)

def initialize_client():
    """Initializes and returns the Google Translate client instance."""
    try:
        client = translate.Client()
        # Test the connection by listing languages
        client.get_languages()
        print("Successfully connected to Google Translate API.")
        return client
    except Exception as e:
        cred_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
        error_message = f"Google Translate initialization failed: {e}. "
        if cred_path:
            error_message += f"The path specified in GOOGLE_APPLICATION_CREDENTIALS ('{cred_path}') might be incorrect or the file is not accessible."
        else:
            error_message += "The GOOGLE_APPLICATION_CREDENTIALS environment variable is not set."

        print(f"Error: Could not initialize Google Translate client: {e}")
        print("Please ensure you have authenticated correctly and the 'GOOGLE_APPLICATION_CREDENTIALS' environment variable is set to a valid file path.")
        logging.error(error_message)
        return None

def protect_terminology(text, terminology):
    """Replaces known terms with placeholders to protect them during translation."""
    placeholders = {}
    for i, (en_term, ka_term) in enumerate(terminology.items()):
        # Using a simple, unique placeholder format
        placeholder = f"<span class=\"notranslate\">term{i}</span>"
        if en_term in text:
            text = text.replace(en_term, placeholder)
            placeholders[placeholder] = ka_term
    return text, placeholders

def unprotect_terminology(text, placeholders):
    """Replaces placeholders back with the correct Georgian terminology."""
    for placeholder, ka_term in placeholders.items():
        text = text.replace(placeholder, ka_term)
    return text


def protect_code_identifiers(text):
    """
    Finds code-like identifiers followed by a type in parentheses and wraps
    the identifier in a 'notranslate' span.
    e.g., "mcpwm_init (C++ function)" -> "<span class='notranslate'>mcpwm_init</span> (C++ function)"
    """
    # This pattern looks for a word that is likely a C/C++ identifier,
    # followed by a space and a parenthesis containing the type.
    pattern = re.compile(r'(\b[a-zA-Z_][a-zA-Z0-9_.:]*\b)(\s*\(C(?:\+\+)?\s(?:macro|function|class|member|enumerator|type)\))')

    def repl(m):
        identifier = m.group(1)
        type_info = m.group(2)
        return f'<span class="notranslate">{identifier}</span>{type_info}'

    return pattern.sub(repl, text)


def translate_batch_with_retry(client, texts, terminology):
    """Translates a batch of texts using Google Translate API with retry logic."""
    if not texts:
        return [], 0

    # 1. Protect terminology and code
    protected_texts = []
    placeholders_list = []
    total_char_count = 0
    for text in texts:
        # First, protect specific code patterns found in indices
        processed_text = protect_code_identifiers(text)
        # Then, protect general terminology from the JSON file
        protected_text, placeholders = protect_terminology(processed_text, terminology)
        protected_texts.append(protected_text)
        placeholders_list.append(placeholders)
        total_char_count += len(protected_text)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 2. Translate using the API
            results = client.translate(
                protected_texts,
                target_language=TARGET_LANG,
                source_language='en',
                format_='html'
            )

            # 3. Restore terminology
            final_texts = []
            for i, result in enumerate(results):
                translated_text = result['translatedText']
                final_text = unprotect_terminology(translated_text, placeholders_list[i])
                final_texts.append(final_text)

            return final_texts, total_char_count

        except google_exceptions.GoogleAPICallError as e:
            print(f"\nWarning: Google API error on batch. Retrying in {5 * (attempt + 1)} seconds...")
            logging.warning(f"Google API error on batch: {e}. Retrying...")
            time.sleep(5 * (attempt + 1))

    logging.error(f"Failed to translate batch after {max_retries} attempts. First text: {texts[0][:100]}...")
    # Return original texts and 0 chars if all retries fail
    return texts, 0

def translate_text_with_retry(client, text, terminology):
    """Translates a single chunk of text. DEPRECATED in favor of batching."""
    return translate_batch_with_retry(client, [text], terminology)


def process_html_file(client, file_path, output_path, terminology):
    """Reads, translates, and saves a single HTML file. Returns character count."""
    total_chars_in_file = 0
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            soup = BeautifulSoup(f, 'html.parser')

        text_nodes = soup.find_all(string=True)
        
        translatable_nodes = []
        for node in text_nodes:
            # Check if the node is a NavigableString and has a parent
            if isinstance(node, NavigableString) and node.parent:
                 # Check if the parent is a translatable tag and not inside an excluded tag
                if (node.parent.name in TRANSLATABLE_TAGS and 
                    all(p.name not in EXCLUDED_TAGS for p in node.find_parents())):
                    if node.strip():
                        translatable_nodes.append(node)

        for node in translatable_nodes:
            original_text = str(node)
            translated_text, char_count = translate_text_with_retry(client, original_text, terminology)
            total_chars_in_file += char_count
            node.replace_with(NavigableString(translated_text))

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(str(soup))
            
        return total_chars_in_file

    except Exception as e:
        print(f"Error processing file {file_path}: {e}")
        logging.error(f"Failed to process {file_path}: {e}")
        return 0

def get_html_files(directory):
    """Recursively finds all HTML files in a directory."""
    html_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".html"):
                html_files.append(os.path.join(root, file))
    return html_files

def read_and_prepare_soup(filepath):
    """Reads an HTML file and returns a BeautifulSoup object and character count."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            soup = BeautifulSoup(content, 'html.parser')
            # Simple character count for estimation
            text_to_translate = soup.get_text()
            return soup, len(text_to_translate)
    except Exception as e:
        print(f"Error reading file {filepath}: {e}")
        logging.error(f"Could not read or parse {filepath}: {e}")
        return None, 0

def translate_soup_content(soup, client, terminology, char_limit_tracker):
    """Translates the content of a BeautifulSoup object."""
    translated_chars_total = 0
    BATCH_SIZE = 128 # Number of text nodes to translate in one API call

    try:
        text_nodes = soup.find_all(string=True)
        translatable_nodes = []
        for node in text_nodes:
            if isinstance(node, NavigableString) and node.parent and \
               node.parent.name in TRANSLATABLE_TAGS and \
               all(p.name not in EXCLUDED_TAGS for p in node.find_parents()):
                if node.strip():
                    translatable_nodes.append(node)

        # Process nodes in batches
        for i in range(0, len(translatable_nodes), BATCH_SIZE):
            batch_nodes = translatable_nodes[i:i + BATCH_SIZE]
            original_texts = [str(node) for node in batch_nodes]

            if char_limit_tracker['count'] >= CHAR_LIMIT_PER_MONTH:
                break

            translated_texts, char_count = translate_batch_with_retry(client, original_texts, terminology)
            char_limit_tracker['count'] += char_count
            translated_chars_total += char_count

            # Replace node content with translated text
            for j, node in enumerate(batch_nodes):
                node.replace_with(NavigableString(translated_texts[j]))

        return soup, translated_chars_total
    except Exception as e:
        logging.error(f"Error during soup translation: {e}", exc_info=True)
        return None, 0

def write_translated_file(soup, original_filepath):
    """Writes the translated soup to the corresponding output file."""
    try:
        relative_path = os.path.relpath(original_filepath, SOURCE_DIR)
        output_path = os.path.join(TRANSLATED_DIR, relative_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(str(soup))
    except Exception as e:
        logging.error(f"Failed to write translated file for {original_filepath}: {e}")

# --- MAIN EXECUTION ---

def main():
    """Main function to orchestrate the translation process."""
    char_limit_tracker = {'count': 0} # Use a dictionary to make it mutable

    # Initialization
    print("--- Initializing Translation Script ---")
    client = initialize_client()
    if not client:
        return # Stop if client fails to initialize

    terminology = load_terminology(TERMINOLOGY_FILE)
    processed_files = load_processed_files(STATE_FILE)
    all_html_files = get_html_files(SOURCE_DIR)

    print(f"Found {len(all_html_files)} total HTML files.")
    print(f"Found {len(processed_files)} already processed files.")

    files_to_process = [f for f in all_html_files if f not in processed_files]

    if not files_to_process:
        print("All files have already been translated. Nothing to do.")
        return

    print(f"Starting translation for {len(files_to_process)} new files.")
    print("-----------------------------------------")

    # Main processing loop
    pbar = tqdm(files_to_process, desc="Translating HTML files")
    for filepath in pbar:
        try:
            print(f"\n[BEGIN] Processing: {os.path.basename(filepath)}")
            pbar.set_description(f"Reading {os.path.basename(filepath)}")

            if char_limit_tracker['count'] >= CHAR_LIMIT_PER_MONTH:
                print("\nCharacter limit reached. Stopping translation.")
                logging.warning("Character limit reached. Stopping.")
                break

            # Read and prepare soup
            soup, total_chars_in_file = read_and_prepare_soup(filepath)
            if soup is None:
                continue

            print(f"File read successfully. Characters to translate: {total_chars_in_file}")

            # Translate content
            pbar.set_description(f"Translating {os.path.basename(filepath)}")
            print("Calling Google Translate API...")
            translated_soup, _ = translate_soup_content(
                soup, client, terminology, char_limit_tracker
            )
            print("API call finished.")

            if translated_soup is None:
                print(f"Skipping file {filepath} due to translation error.")
                continue

            # Write translated file
            pbar.set_description(f"Writing {os.path.basename(filepath)}")
            print("Writing translated file...")
            write_translated_file(translated_soup, filepath)
            print("Write operation successful.")

            # Update state
            processed_files.add(filepath)
            save_processed_files(STATE_FILE, processed_files)
            print(f"[END] Successfully processed: {os.path.basename(filepath)}")
            print(f"Total characters translated so far: {char_limit_tracker['count']}")
            print("-----------------------------------------")

        except Exception as e:
            print(f"\nAn unexpected error occurred while processing {filepath}: {e}")
            logging.error(f"CRITICAL ERROR on {filepath}: {e}", exc_info=True)
            continue # Move to the next file

    print("\n--- Translation Process Finished ---")
    print(f"Total files processed in this session: {pbar.n}")
    print(f"Total characters translated: {char_limit_tracker['count']}")


if __name__ == "__main__":
    main()

