import webbrowser
import urllib.parse

def execute(args):
    """Takes JSON arguments from I.R.I.S. and opens a browser."""
    query = args.get("query")
    
    if query:
        # Convert normal text into a URL format (e.g., "rain news" -> "rain+news")
        safe_query = urllib.parse.quote_plus(query)
        url = f"https://www.google.com/search?q={safe_query}"
    else:
        # Fallback if she doesn't provide a specific query
        url = "https://www.google.com"

    print(f"🌍 [SKILL EXECUTED: Opening browser to {url}]")
    
    # This built-in command tells Windows to open the default browser
    webbrowser.open(url)
    
    return f"I have opened the browser and searched for '{query}'."