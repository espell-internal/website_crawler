All you need to change in this code before you run it, is change the word 'website' in the code to the website you want to crawl, and '.domain' to the domain.

If you want it to save the files next to the script instead of the root, change OUTPUT_DIR = Path("website_crawl") to OUTPUT_DIR = Path(__file__).resolve().parent / "website_crawl"
