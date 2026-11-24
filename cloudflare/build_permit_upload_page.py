"""Build/check the maintained permits PAGE_HTML constant; never emits a full Worker."""
import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
HEADLINE = 'Newsroom building and zoning permit uploads'
TITLE = HEADLINE + ' | La Crosse Tribune'

class Headings(HTMLParser):
    def __init__(self):
        super().__init__(); self.current = None; self.text = []; self.values = {'h1': [], 'title': []}
    def handle_starttag(self, tag, attrs):
        if tag in self.values:self.current = tag; self.text = []
    def handle_data(self, data):
        if self.current:self.text.append(data)
    def handle_endtag(self, tag):
        if tag == self.current:
            self.values[tag].append(' '.join(''.join(self.text).split())); self.current = None

def validate(page):
    parser = Headings(); parser.feed(page); parser.close()
    if parser.values != {'h1': [HEADLINE], 'title': [TITLE]}:
        raise ValueError('The permits uploader must preserve its approved headline and title.')
    if 'Newsroom report uploads' in page:
        raise ValueError('The retired permits uploader headline must not be restored.')

def constant(page):
    validate(page)
    return 'const PAGE_HTML = ' + json.dumps(page, ensure_ascii=False) + ';\n'

def check_worker(path, page):
    # Dashboard code supplies PAGE_HTML as a JSON-encoded JavaScript string.
    # Refuse unrecognized declarations instead of guessing at executable JS.
    match = re.search(r'\bconst\s+PAGE_HTML\s*=\s*("(?:[^"\\]|\\.)*")\s*;', Path(path).read_text())
    if not match:raise ValueError('Candidate Worker has no recognized PAGE_HTML constant.')
    actual = json.loads(match[1]); validate(actual)
    if actual != page:raise ValueError('Candidate Worker does not embed the maintained permits page. Rebuild it first.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check the saved constant without changing files.')
    parser.add_argument('--check-worker', type=Path, help='Check the PAGE_HTML embedded in a candidate full Worker.')
    args = parser.parse_args()
    page = (ROOT / 'permit-upload-page.html').read_text(); expected = constant(page)
    artifact = ROOT / 'permit-upload-page.constant.js'
    if args.check or args.check_worker:
        if artifact.read_text() != expected:raise ValueError('Saved page constant is stale; rebuild it.')
        if args.check_worker:check_worker(args.check_worker, page)
        print('Permits uploader headline, title and template checks passed.')
    else:
        artifact.write_text(expected)
        print('Generated PAGE_HTML constant only. Keep the existing full Worker backend.')

if __name__ == '__main__':
    try:main()
    except (ValueError, OSError) as error:raise SystemExit(str(error))
