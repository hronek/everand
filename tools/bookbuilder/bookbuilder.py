#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Tuple, Optional, Dict

import requests
import time
from bs4 import BeautifulSoup
from ebooklib import epub, ITEM_IMAGE
import html as html_lib
from lxml import html as lxml_html, etree as lxml_etree

# Lazy import for translator to avoid hard dependency when not used
def _get_google_translator(source_lang: str, target_lang: str):
    try:
        from deep_translator import GoogleTranslator  # type: ignore
    except Exception as e:
        print(f"[TRANS] Nelze načíst deep-translator: {e}. Přeskakuji překlad.")
        return None
    try:
        return GoogleTranslator(source=source_lang or 'auto', target=target_lang)
    except Exception as e:
        print(f"[TRANS] Nelze inicializovat GoogleTranslator: {e}. Přeskakuji překlad.")
        return None

def _translate_texts(translator, texts: List[str]) -> List[str]:
    """Translate a list of texts using deep-translator with retries.
    If any item fails after retries, raise RuntimeError to abort the whole process.
    """
    if translator is None:
        return texts
    # First try batch translate with retries (if available)
    if hasattr(translator, 'translate_batch'):
        last_err = None
        for attempt in range(5):
            try:
                res = translator.translate_batch(texts)
                return list(res)
            except Exception as e:
                last_err = e
                # Exponential backoff up to ~10s total
                time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"[TRANS] Selhal batch překlad po opakovaných pokusech: {last_err}")
    # Fallback: translate one-by-one with retries
    out: List[str] = []
    for idx, t in enumerate(texts):
        success = False
        last_err = None
        for attempt in range(5):
            try:
                out.append(translator.translate(t))
                success = True
                break
            except Exception as e:
                last_err = e
                time.sleep(0.4 * (2 ** attempt))
        if not success:
            raise RuntimeError(f"[TRANS] Selhal překlad položky #{idx+1}/{len(texts)}: {last_err}")
    return out

def translate_soup_in_place(soup: BeautifulSoup, translator) -> None:
    """Translate visible text nodes in common block-level tags in-place.
    Keeps HTML structure; only replaces text content. Images/links preserved.
    """
    if translator is None:
        return
    targets = ("h1","h2","h3","h4","h5","h6","p","li","blockquote","figcaption","caption","span","div")
    nodes = []
    texts = []
    for tag in soup.find_all(targets):
        # Skip tags that contain only images
        try:
            if tag.find("img") and not (tag.get_text(strip=True) or ""):
                continue
        except Exception:
            pass
        txt = tag.get_text(" ", strip=True)
        if txt:
            nodes.append(tag)
            texts.append(txt)
    if not texts:
        return
    translated = _translate_texts(translator, texts)
    # Assign back: replace inner text but keep children; simplest is to set tag.string
    for tag, new_txt in zip(nodes, translated):
        try:
            # Remove all children and set pure text to keep it simple and robust
            for c in list(tag.contents):
                c.extract()
            tag.string = new_txt
        except Exception:
            continue


def human_sort_key(p: Path) -> Tuple:
    # Natural sort by name (handles (1), (2) ... (10))
    def atoi(text):
        return int(text) if text.isdigit() else text.lower()
    return tuple(atoi(c) for c in re.split(r"(\d+)", p.name))


def list_html_files(input_dir: Path, sort: str) -> List[Path]:
    # Hledáme jak .html tak .HTML soubory
    files = list(input_dir.glob("*.[hH][tT][mM][lL]"))
    if not files:
        # Zkusíme alternativní metodu pro Windows, kde glob může mít problémy s diakritikou
        try:
            files = [input_dir / f for f in os.listdir(input_dir) 
                    if f.lower().endswith('.html') and os.path.isfile(input_dir / f)]
        except Exception as e:
            print(f"[CHYBA] Nelze načíst soubory z {input_dir}: {e}")
            return []
    
    if sort == "name":
        files.sort(key=human_sort_key)
    else:
        # default: creation time (ctime)
        files.sort(key=lambda p: p.stat().st_ctime)
    
    return files


def clean_html_keep_structure(html_content: str) -> BeautifulSoup:
    # First, remove the title text pattern that appears in the content
    import re
    title_patterns = [
        r'Il Cammino Neocatecumenale_? Storia e pratica religiosa \(Vol\. I\)(?:\.html)?(?: \(\d+\))?',
        r'Il Cammino Neocatecumenale: Storia e pratica religiosa \(Vol\. I\)',
        r'Il Cammino Neocatecumenale',
        r'Storia e pratica religiosa',
        r'Vol\.? I',
        r'HighlightDelete Add Note Share Quote',
        r'Highlight',
        r'Delete',
        r'Add Note',
        r'Share Quote'
    ]
    for pattern in title_patterns:
        html_content = re.sub(pattern, '', html_content, flags=re.IGNORECASE)

    soup = BeautifulSoup(html_content, "lxml")

    # Remove scripts, noscript, iframes, embeds
    for tag in soup(["script", "noscript", "iframe", "embed", "object"]):
        tag.decompose()

    # Remove link tags except stylesheets (we'll still strip external CSS later)
    for link in soup.find_all("link"):
        rel = (link.get("rel") or [])
        if "stylesheet" not in rel:
            link.decompose()

    # Remove inline styles-heavy style tags; we will inject our own minimal CSS
    for style in soup.find_all("style"):
        style.decompose()

    # Heuristic: detect main content container to protect it from over-cleaning
    main_root = None
    try:
        candidates = soup.find_all(["article", "main", "div", "section"]) or []
        best_score = -1
        for c in candidates:
            p_count = len(c.find_all("p"))
            txt_len = len((c.get_text(" ", strip=True) or ""))
            score = (p_count * 200) + txt_len
            if score > best_score:
                best_score = score
                main_root = c
        # Require some minimum to count as main
        if main_root:
            if (len(main_root.find_all("p")) < 3) and (len((main_root.get_text(" ", strip=True) or "")) < 800):
                main_root = None
    except Exception:
        main_root = None

    # Remove obvious layout chrome: headers/footers/nav/aside/forms/buttons/svg
    for tag in soup.find_all(["header", "footer", "nav", "aside", "form", "button", "svg"]):
        # Keep if it's the detected main content itself
        if main_root and (tag is main_root or main_root in tag.descendants):
            continue
        tag.decompose()

    # Remove common reader UI blocks by class/id heuristics (narrow set)
    chrome_keywords = (
        "toolbar",
        "pagination",
        "pager",
        "rating",
        "review",
        "message",
        "notification",
        "cta",
        "book-navigation",
        "book_nav",
        "breadcrumb",
        "sidebar",
        "overlay",
        "modal",
        "banner",
        "cookie",
        "progressbar",
        "progress-bar",
    )
    for el in soup.find_all(True):
        attrs = getattr(el, 'attrs', {}) or {}
        cid_val = attrs.get('id', '')
        if cid_val is None:
            cid_val = ''
        cid = " ".join(str(cid_val).split()).lower()
        cls_val = attrs.get('class', [])
        if isinstance(cls_val, list):
            cls = " ".join([str(x) for x in cls_val]).lower()
        else:
            cls = str(cls_val or '').lower()
        if el.name in ("div", "nav", "aside", "section", "header", "footer") and any(k in cid or k in cls for k in chrome_keywords):
            if main_root and (el is main_root or main_root in el.descendants or el in getattr(main_root, 'descendants', [])):
                # Don't remove containers that are or contain the main content
                pass
            else:
                # Heuristic guard: keep if it looks like main content
                text_len = len((el.get_text(" ", strip=True) or ""))
                p_count = len(el.find_all("p"))
                img_count = len(el.find_all("img"))
                if p_count >= 3 or text_len > 500:
                    pass
                elif img_count > 0:
                    # Preserve containers that include images (e.g., cover or chapter images)
                    pass
                else:
                    try:
                        el.decompose()
                    except Exception:
                        pass
    # Remove empty or whitespace-only elements that might create empty pages
    for element in soup.find_all(True):
        if element.name in ['div', 'p', 'span'] and not element.get_text(strip=True) and not element.find_all(['img', 'table']):
            element.decompose()

    # Remove known noise phrases by nuking their nearest block container
    import re as _re_noise
    
    # First, remove the exact title text if it appears as direct text content
    title_text = "Il Cammino Neocatecumenale: Storia e pratica religiosa (Vol. I)"
    for element in soup.find_all(string=lambda text: text and title_text in text):
        try:
            element.replace_with(element.replace(title_text, ''))
        except Exception:
            pass
    
    noise_phrases = [
        r"^\s*currently\s+reading\b",
        r"\bdismiss\s+message\b",
        r"\benjoying\s+this\s+book\?\b",
        r"\bprevious\s+page\b",
        r"\bnext\s+page\b",
        r"\byou've\s+reached\s+the\s+end\b",
        r"\bleave\s+a\s+rating\b",
        r"\bwrite\s+a\s+review\b",
        r"\bthis\s+book\s+failed\s+to\s+load\b",
        r"\bsomething\s+is\s+not\s+right\b",
        r"\bbook\s+navigation\b",
        r"\bpage\s+\d+\s+of\s+\d+\b",
        r"\b%\s*read\b",
        r"\bhighlight\b",
        r"\bdelete\b",
        r"\badd\s+note\b",
        r"\bshare\s+quote\b",
        r"^\s*introduzione\b",
        r"\bintroduzione\b",
    ]
    noise_re = _re_noise.compile("|".join(noise_phrases), _re_noise.I)
    for t in soup.find_all(string=noise_re):
        # Manually traverse parents to avoid calling find_parent on NavigableString
        parent = getattr(t, 'parent', None)
        blk = None
        cur = parent
        while cur is not None:
            try:
                name = getattr(cur, 'name', None)
            except Exception:
                name = None
            if name in ("section", "div", "nav", "header", "footer", "aside"):
                blk = cur
                break
            cur = getattr(cur, 'parent', None)
        if blk is None:
            blk = parent
        # If the immediate parent is a span styled as block, prefer removing that span
        try:
            if parent is not None and getattr(parent, 'name', None) == 'span':
                style = str(parent.get('style', '')).lower()
                if 'display:block' in style or 'display: block' in style:
                    blk = parent
        except Exception:
            pass
        # Guard: only remove whole block if small (likely UI), else just remove the text node
        try:
            blk_text_len = len((blk.get_text(" ", strip=True) or "")) if blk else 0
            blk_p_count = len(blk.find_all("p")) if blk else 0
        except Exception:
            blk_text_len, blk_p_count = 0, 0
        # Do not remove noise blocks that include images (to keep covers/figures)
        blk_img_count = 0
        try:
            blk_img_count = len(blk.find_all("img")) if blk else 0
        except Exception:
            blk_img_count = 0
        if blk and (not main_root or (blk is not main_root and main_root not in blk.descendants)) and blk_p_count <= 1 and blk_text_len < 400 and blk_img_count == 0:
            try:
                blk.decompose()
                continue
            except Exception:
                pass
        try:
            t.extract()
        except Exception:
            pass

    # Pass 3: within containers that include images, drop absolutely-positioned non-image overlays
    try:
        for container in soup.find_all(True):
            try:
                if container.find("img") is None:
                    continue
            except Exception:
                continue
            for child in list(container.find_all(True, recursive=True)):
                if child.name == "img":
                    continue
                style = str(child.get("style", "")).lower()
                if ("position:absolute" in style) or ("position: absolute" in style) or ("position:fixed" in style) or ("position: fixed" in style):
                    try:
                        if child.find("img") is None:
                            child.decompose()
                    except Exception:
                        pass
    except Exception:
        pass

    # Pass 4b: remove utility-only lists anywhere (not only top-level)
    try:
        util_words = {"highlight", "delete", "add note", "share quote"}
        for lst in list(soup.find_all(["ul", "ol"])):
            try:
                links = lst.find_all("a")
                link_texts = { (a.get_text(" ", strip=True) or "").lower() for a in links }
                text_len = len((lst.get_text(" ", strip=True) or ""))
                if (link_texts and link_texts.issubset(util_words)) or (len(links) <= 3 and text_len < 120):
                    # ensure not part of main content
                    if not main_root or (main_root not in lst.descendants and lst is not main_root):
                        lst.decompose()
            except Exception:
                pass
    except Exception:
        pass

    # Pass 4c: remove early short headings duplicating the book title fragment even if nested
    try:
        title_re = __import__('re').compile(r"il\s+cammino\s+neocatecumenale", __import__('re').I)
        body = soup.body or soup
        # Traverse first ~200 elements depth-first
        count = 0
        for el in body.descendants:
            if count > 200:
                break
            if not hasattr(el, 'name'):
                continue
            count += 1
            if el.name in ("h1", "h2", "h3", "div", "p", "span"):
                txt = (el.get_text(" ", strip=True) or "").lower()
                if txt and len(txt) < 200 and title_re.search(txt):
                    # avoid removing if element contains images
                    try:
                        if el.find("img") is None:
                            el.decompose()
                    except Exception:
                        pass
    except Exception:
        pass

    # Pass 4: trim top-level navigation lists with many links and little text
    try:
        body = soup.body or soup
        top_blocks = list(getattr(body, 'contents', []) or [])[:10]
        for el in top_blocks:
            if not hasattr(el, 'name'):
                continue
            if el.name in ("ul", "ol"):
                links = el.find_all("a")
                text_len = len((el.get_text(" ", strip=True) or ""))
                # Remove pure utility lists
                util_words = {"highlight", "delete", "add note", "share quote"}
                link_texts = { (a.get_text(" ", strip=True) or "").lower() for a in links }
                only_utils = bool(link_texts) and link_texts.issubset(util_words)
                if (len(links) >= 3 and text_len < 300) or only_utils or (len(links) <= 3 and text_len < 120):
                    try:
                        el.decompose()
                    except Exception:
                        pass
            # Remove duplicate title heading even if not the very first node
            if el.name in ("h1", "h2", "div", "p"):
                head_txt = (el.get_text(" ", strip=True) or "").lower()
                if "il cammino neocatecumenale" in head_txt and len(head_txt) < 200:
                    try:
                        el.decompose()
                    except Exception:
                        pass
    except Exception:
        pass

    # Pass 5: drop duplicate leading book title heading for this title (site chrome)
    try:
        body = soup.body or soup
        top = None
        for node in getattr(body, 'contents', []) or []:
            if hasattr(node, 'name') and node.name in ("h1", "h2", "div", "p"):
                top = node
                break
        if top:
            text = (top.get_text(" ", strip=True) or "").lower()
            if "il cammino neocatecumenale" in text and len(text) < 200:
                try:
                    top.decompose()
                except Exception:
                    pass
    except Exception:
        pass

    # Pass 6: remove standalone INTRODUZIONE heading at the start of pages
    try:
        body = soup.body or soup
        first_blocks = list(getattr(body, 'contents', []) or [])[:8]
        for node in first_blocks:
            if not hasattr(node, 'name'):
                continue
            if node.name in ("h1", "h2", "h3", "div", "p"):
                txt = (node.get_text(" ", strip=True) or "").strip()
                if txt.upper() == "INTRODUZIONE" or txt.lower().startswith("introduzione"):
                    try:
                        node.decompose()
                    except Exception:
                        pass
                    break
    except Exception:
        pass

    # Remove attributes that often come from saved pages / trackers
    for tag in soup.find_all(True):
        # drop data-*, on*, class bloats (keep minimal classes to preserve basic layout when meaningful)
        to_del = []
        for attr in list(tag.attrs.keys()):
            if attr.startswith("data-") or attr.startswith("on"):
                to_del.append(attr)
        for a in to_del:
            try:
                del tag[a]
            except Exception:
                pass

    return soup


def extract_title(soup: BeautifulSoup, fallback: str) -> str:
    # Prefer first h1 text
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    # Fallback: document title
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    # Fallback: filename
    return fallback


def download_image(url: str, session: requests.Session) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        # Guess extension from content-type
        ct = r.headers.get("Content-Type", "").lower()
        if "jpeg" in ct or "jpg" in ct:
            ext = ".jpg"
        elif "png" in ct:
            ext = ".png"
        elif "gif" in ct:
            ext = ".gif"
        elif "webp" in ct:
            ext = ".webp"
        elif "svg" in ct:
            ext = ".svg"
        else:
            # fallback try from url
            from_urlext = Path(url).suffix
            ext = from_urlext if from_urlext.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"} else ".bin"
        return r.content, ext
    except Exception:
        return None, None


def embed_images_and_rewrite(soup: BeautifulSoup, book: epub.EpubBook, session: requests.Session, img_prefix: str) -> Tuple[BeautifulSoup, Dict[str, epub.EpubItem]]:
    img_map: Dict[str, epub.EpubItem] = {}
    counter = 1

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        # Ignore tiny tracking images by heuristic
        width = img.get("width")
        height = img.get("height")
        try:
            if width and int(width) <= 1:
                continue
            if height and int(height) <= 1:
                continue
        except Exception:
            pass

        if src.startswith("data:"):
            # Keep data URIs as-is by converting to binary asset
            try:
                header, b64data = src.split(",", 1)
                mime = header.split(";")[0].split(":")[1]
                import base64
                content = base64.b64decode(b64data)
                if "/jpeg" in mime or "/jpg" in mime:
                    ext = ".jpg"
                elif "/png" in mime:
                    ext = ".png"
                elif "/gif" in mime:
                    ext = ".gif"
                elif "/webp" in mime:
                    ext = ".webp"
                elif "/svg" in mime:
                    ext = ".svg"
                else:
                    ext = ".bin"
                fname = f"{img_prefix}{counter}{ext}"
                item = epub.EpubItem(file_name=f"images/{fname}", content=content, media_type=mime)
                book.add_item(item)
                img["src"] = f"images/{fname}"
                img_map[src] = item
                counter += 1
            except Exception:
                continue
        else:
            # Absolute or relative URL: try to download
            content, ext = download_image(src, session)
            if content is None:
                # attempt fix for protocol-relative //
                if src.startswith("//"):
                    content, ext = download_image("https:" + src, session)
            if content is None:
                continue
            # Determine media type
            media = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".svg": "image/svg+xml",
            }.get(ext.lower(), "application/octet-stream")
            fname = f"{img_prefix}{counter}{ext}"
            item = epub.EpubItem(file_name=f"images/{fname}", content=content, media_type=media)
            book.add_item(item)
            img["src"] = f"images/{fname}"
            img_map[src] = item
            counter += 1

    return soup, img_map


def pick_cover_from_first_image(soup: BeautifulSoup, book: epub.EpubBook, session: requests.Session) -> Optional[epub.EpubItem]:
    first_img = soup.find("img")
    if not first_img:
        return None
    src = first_img.get("src")
    if not src:
        return None
    # Try to obtain binary content
    if src.startswith("data:"):
        try:
            header, b64data = src.split(",", 1)
            import base64
            content = base64.b64decode(b64data)
            media = header.split(";")[0].split(":")[1]
        except Exception:
            return None
    else:
        content, ext = download_image(src, session)
        if content is None:
            return None
        media = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(ext.lower(), "image/jpeg")

    cover_item = epub.EpubItem(file_name="images/cover.jpg", content=content, media_type=media)
    book.add_item(cover_item)
    try:
        book.set_cover("cover.jpg", content)
    except Exception:
        pass
    return cover_item


def load_custom_css(css_path: Optional[Path]) -> str:
    if css_path and css_path.exists():
        return css_path.read_text(encoding="utf-8")
    # minimal default CSS
    return (
        "body{font-family:serif;line-height:1.4;margin:0 1em;}\n"
        "img{max-width:100%;height:auto;}\n"
        "h1{page-break-before:always;font-size:1.6em;margin:1em 0;}\n"
        "h2{font-size:1.3em;margin:1em 0;}\n"
        "p{margin:0.6em 0;}\n"
    )


def prompt_metadata(args) -> Tuple[str, str, str]:
    # Ask interactively if not provided; user requested to be asked on start
    print("Zadejte metadata knihy. Necháte-li prázdné, použijí se rozumné výchozí hodnoty.")
    title = input(f"Název knihy [{args.title or 'Moje kniha'}]: ").strip() or (args.title or "Moje kniha")
    author = input(f"Autor [{args.author or 'Unknown'}]: ").strip() or (args.author or "Unknown")
    lang = input(f"Jazyk (cs,en,it,...) [{args.lang or 'cs'}]: ").strip() or (args.lang or "cs")
    return title, author, lang


def build_book(
    input_dir: Path,
    epub_output: Optional[Path],
    sort: str,
    title: str,
    author: str,
    lang: str,
    css_path: Optional[Path],
    ask_metadata: bool,
    pdf_output: Optional[Path],
    wkhtmltopdf_path: Optional[Path],
    pdf_dump_path: Optional[Path],
    lang_out: Optional[str] = None,
) -> None:
    files = list_html_files(input_dir, sort)
    if not files:
        print("Nenalezeny žádné HTML soubory.")
        sys.exit(1)

    # Prepare book
    book = epub.EpubBook()

    if ask_metadata:
        title, author, lang = prompt_metadata(argparse.Namespace(title=title, author=author, lang=lang))

    book.set_title(title)
    book.add_author(author)
    book.set_language(lang)

    css_text = load_custom_css(css_path)
    style_item = epub.EpubItem(file_name="style/style.css", content=css_text, media_type="text/css")
    book.add_item(style_item)

    session = requests.Session()

    chapters = []
    spine_items = ["nav"]

    cover_set = False

    # For PDF: collect validated chapter HTML bodies with a flag whether to include heading
    pdf_chapters: List[Tuple[str, str, bool]] = []

    chap_index = 0
    file_idx = 0
    # Initialize translator if requested
    translator = None
    lang_out_norm = (lang_out or '').strip().lower() or None
    if lang_out_norm and (lang_out_norm != (lang or '').strip().lower()):
        translator = _get_google_translator(source_lang=(lang or 'auto'), target_lang=lang_out_norm)
        if translator is None:
            print("[TRANS] Požadován překlad (--lang-out), ale překladač nelze inicializovat. Ukončuji bez výstupu.")
            sys.exit(3)

    for file in files:
        file_idx += 1
        html = file.read_text(encoding="utf-8", errors="ignore")
        soup = clean_html_keep_structure(html)

        # Embed images and rewrite src
        # Use per-file unique prefix to avoid duplicate names across EPUB
        soup, _ = embed_images_and_rewrite(soup, book, session, img_prefix=f"img_{file_idx}_")

        # Optional: translate content to output language
        if translator is not None:
            translate_soup_in_place(soup, translator)

        # Title for chapter (after possible translation)
        # Only include H1 in PDF if it exists in source; otherwise avoid filename/title fallback as heading
        try:
            _h1_tag = soup.find("h1")
            _h1_text = _h1_tag.get_text(strip=True) if _h1_tag else ""
        except Exception:
            _h1_text = ""
        chap_title = _h1_text if _h1_text else extract_title(soup, fallback=file.stem)
        include_h1_in_pdf = bool(_h1_text)

        # On first file, pick cover from first image if available
        if not cover_set:
            cover_item = pick_cover_from_first_image(soup, book, session)
            cover_set = cover_item is not None

        # Build EpubHtml
        body = soup.body or soup
        # Determine if body has meaningful content
        body_html = str(body).strip()
        # Remove surrounding <body> wrapper if present to test emptiness of inner content
        if body.name == "body":
            inner = "".join(str(c) for c in body.contents).strip()
        else:
            inner = body_html

        # Stronger emptiness/junk check: ignore pure whitespace/comments and UI-only leftovers
        import re as _re
        has_visible = bool(_re.search(r"<(img|p|h1|h2|h3|h4|h5|h6|ul|ol|li|a|blockquote|table)\b", inner, _re.I)) or bool(_re.search(r"\w", inner))
        # Compute plain text and element counts
        try:
            _bs_tmp = BeautifulSoup(inner, "lxml")
            plain = (_bs_tmp.get_text(" ", strip=True) or "")
            has_img = _bs_tmp.find("img") is not None
            p_count = len(_bs_tmp.find_all("p"))
            a_count = len(_bs_tmp.find_all("a"))
        except Exception:
            plain, has_img, p_count, a_count = inner, False, 0, 0

        # If we have substantial plain text, force-keep even if structure is minimal
        forced_keep = len(plain) >= 200

        # UI-only/junk patterns (title/header + utility links)
        junk_re = _re.compile(r"^(?:\s*(il\s+cammino\s+neocatecumenale[^\n]*|highlight|delete|add\s+note|share\s+quote|introduzione)\s*)+$", _re.I)
        looks_junk = (not has_img) and (len(plain) < 240) and junk_re.match(plain or "") is not None
        # Also treat as junk if very short, few/no paragraphs, and only a few links
        very_short = (not has_img) and (len(plain) < 160) and (p_count == 0) and (a_count <= 5)
        # If the HTML <title> is the book title and body is short without images, skip page (likely site header page)
        try:
            doc_title = (soup.title.get_text(" ", strip=True) or "").lower() if soup.title else ""
        except Exception:
            doc_title = ""
        title_looks_chrome = ("il cammino neocatecumenale" in doc_title)
        body_too_small = (not has_img) and (len(plain) < 400) and (p_count <= 1)
        if not forced_keep and ((not inner or not has_visible) or looks_junk or very_short or (title_looks_chrome and body_too_small)):
            print(f"[SKIP] Prázdný/UI-only obsah po vyčištění: {file.name}")
            continue

        # If content is mostly plain text without block tags, wrap into paragraphs for valid XHTML
        if p_count == 0 and ('<' not in (inner or '') or not _re.search(r"<\s*(p|h1|h2|h3|h4|h5|h6|ul|ol|li|blockquote|table|img)\b", inner or '', _re.I)):
            # Split by double newlines to paragraphs; fallback to one paragraph
            parts_txt = [t.strip() for t in _re.split(r"\n\s*\n", plain) if t.strip()]
            if not parts_txt and plain.strip():
                parts_txt = [plain.strip()]
            inner = "\n".join(f"<p>{html_lib.escape(t)}</p>" for t in parts_txt)

        chap_index += 1
        ch = epub.EpubHtml(title=chap_title, file_name=f"chapters/ch_{chap_index:03}.xhtml", lang=lang)
        # Inject link to our CSS
        # BeautifulSoup will output as text; we add CSS via 'content' and ensure epub links it
        ch_content = """<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html>
<html xmlns=\"http://www.w3.org/1999/xhtml\">
<head>
<meta charset=\"utf-8\"/>
<link rel=\"stylesheet\" type=\"text/css\" href=\"../style/style.css\"/>
<title>{title}</title>
</head>
<body>
{body}
</body>
</html>
""".format(title=html_lib.escape(chap_title), body=inner if body.name == "body" else str(body))
        # Validate XHTML to avoid later lxml ParserError in ebooklib's nav builder
        is_valid = True
        try:
            parser = lxml_html.HTMLParser(encoding='utf-8')
            tree = lxml_html.document_fromstring(ch_content.encode('utf-8'), parser=parser)
            body_node = tree.find('.//body')
            # consider valid if there's at least one non-whitespace char or element of interest
            text_ok = bool((body_node.text or '').strip())
            elems_ok = bool(tree.xpath('//body//*[self::p or self::img or self::h1 or self::h2 or self::h3 or self::ul or self::ol or self::li or self::a or self::blockquote or self::table]'))
            if not (text_ok or elems_ok):
                is_valid = False
        except lxml_etree.ParserError:
            is_valid = False

        if not is_valid:
            print(f"[SKIP] Kapitola není validní XHTML nebo je prázdná po parsování: {file.name}")
            continue

        # Set content explicitly as UTF-8 bytes (more robust for ebooklib)
        ch.set_content(ch_content.encode("utf-8"))

        book.add_item(ch)
        chapters.append(ch)
        spine_items.append(ch)

        # Store content for PDF (use inner/body HTML only, not full XHTML wrapper)
        pdf_chapters.append((chap_title, inner if body.name == "body" else str(body), include_h1_in_pdf))

    if not chapters:
        print("Žádná kapitola s obsahem po vyčištění. Ukončuji.")
        sys.exit(1)

    # TOC: use chapters (H1 per HTML => kapitola)
    book.toc = tuple(chapters)

    # Spine
    book.spine = spine_items

    # Required navigation files
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Write EPUB (only if requested)
    print(f"Kapitoly k zápisu: {len(chapters)}")
    if epub_output is not None:
        epub_output.parent.mkdir(parents=True, exist_ok=True)
        epub.write_epub(str(epub_output), book)
        print(f"Vytvořen EPUB: {epub_output}")

    # Optional PDF generation
    if pdf_output is not None:
        try:
            import pdfkit  # type: ignore
        except Exception as e:
            print("[PDF] pdfkit není nainstalován. Přidejte jej do prostředí: pip install pdfkit")
            return

        # Build single HTML for PDF
        pagebreak_css = "\n.pagebreak{ page-break-before: always; }\n"
        combined_css = load_custom_css(css_path) + pagebreak_css
        parts = [
            "<!DOCTYPE html>",
            "<html>",
            "<head>",
            '<meta charset="utf-8"/>',
            f"<title>{html_lib.escape(title)}</title>",
            "<style>" + combined_css + "</style>",
            "</head>",
            "<body>",
        ]
        first = True
        for chap_title, chap_body, include_h1 in pdf_chapters:
            if not first:
                parts.append('<div class="pagebreak"></div>')
            first = False
            if include_h1:
                parts.append(f"<h1>{html_lib.escape(chap_title)}</h1>")
            parts.append(chap_body)
        parts += ["</body>", "</html>"]
        html_for_pdf = "\n".join(parts)

        # Inline EPUB images as data URIs for wkhtmltopdf to avoid ContentNotFoundError
        try:
            # Build map: file_name -> (mime, base64)
            import base64 as _b64
            img_items = {it.file_name: (getattr(it, 'media_type', 'application/octet-stream'), _b64.b64encode(it.get_content() if hasattr(it, 'get_content') else it.content).decode('ascii'))
                         for it in book.get_items_of_type(ITEM_IMAGE)}

            import re as _re2

            def _norm(path: str) -> str:
                p = path.strip().strip('"').strip("'")
                # drop query/hash
                p = p.split('#', 1)[0].split('?', 1)[0]
                # normalize leading ./ or ../
                while p.startswith('./'):
                    p = p[2:]
                while p.startswith('../'):
                    p = p[3:]
                if p.startswith('/'):
                    p = p.lstrip('/')
                # ensure images/ prefix for lookup
                return p if p.startswith('images/') else f"images/{p}"

            # Replace attributes including SVG and object/data:
            # src, data, data-src, data-original, data-lazy-src, href, xlink:href
            attr_pattern = _re2.compile(r"\b(src|data|data-src|data-original|data-lazy-src|href|xlink:href)\s*=\s*(\"([^\"]+)\"|'([^']+)'|([^\s>]+))", _re2.IGNORECASE)

            def _attr_repl(m: _re2.Match) -> str:
                full = m.group(0)
                val = m.group(3) or m.group(4) or m.group(5) or ''
                key = _norm(val)
                if key in img_items:
                    mime, b64data = img_items[key]
                    return f"{m.group(1)}=\"data:{mime};base64,{b64data}\""
                return full

            html_for_pdf = attr_pattern.sub(_attr_repl, html_for_pdf)

            # Replace CSS url(...) references
            url_pattern = _re2.compile(r"url\(([^)]+)\)", _re2.IGNORECASE)

            def _url_repl(m: _re2.Match) -> str:
                raw = m.group(1).strip().strip('"').strip("'")
                key = _norm(raw)
                if key in img_items:
                    mime, b64data = img_items[key]
                    return f"url(data:{mime};base64,{b64data})"
                return m.group(0)

            html_for_pdf = url_pattern.sub(_url_repl, html_for_pdf)
            # Remove any remaining url(...) that are not data:
            html_for_pdf = _re2.sub(r"url\(((?!data:)[^)]+)\)", "url('')", html_for_pdf, flags=_re2.IGNORECASE)

            # Remove srcset attributes (can contain external URLs that wkhtmltopdf tries to load)
            html_for_pdf = _re2.sub(r"\s+srcset=\"[^\"]*\"", "", html_for_pdf, flags=_re2.IGNORECASE)
            html_for_pdf = _re2.sub(r"\s+srcset='[^']*'", "", html_for_pdf, flags=_re2.IGNORECASE)

            # Neutralize external http(s) and protocol-relative // URLs in src/href after inlining
            def _neutralize_external(m: _re2.Match) -> str:
                full = m.group(0)
                val = (m.group(3) or m.group(4) or m.group(5) or '').strip()
                low = val.lower()
                if low.startswith(('http://', 'https://')) or val.startswith('//'):
                    return f"{m.group(1)}=\"#\""
                # Neutralize any non-data non-anchor leftovers (e.g., unresolved local paths)
                if not low.startswith('data:') and not val.startswith('#') and not low.startswith(('mailto:', 'javascript:')):
                    return f"{m.group(1)}=\"#\""
                return full

            html_for_pdf = attr_pattern.sub(_neutralize_external, html_for_pdf)

            # Remove any <link ...> tags remaining in body content
            html_for_pdf = _re2.sub(r"<link\b[^>]*>", "", html_for_pdf, flags=_re2.IGNORECASE)
        except Exception as e:
            print(f"[PDF] Varování: Nepodařilo se inline-ovat některé obrázky: {e}")

        # Configure wkhtmltopdf if path provided
        config = None
        if wkhtmltopdf_path is not None:
            try:
                config = pdfkit.configuration(wkhtmltopdf=str(wkhtmltopdf_path))
            except Exception as e:
                print(f"[PDF] Nelze použít wkhtmltopdf na {wkhtmltopdf_path}: {e}")
                config = None

        # Vytvoříme minimální konfiguraci pro čistý výstup bez hlaviček a zápatí
        options = {
            'encoding': 'UTF-8',
            'margin-top': '12mm',
            'margin-right': '12mm',
            'margin-bottom': '15mm',
            'margin-left': '12mm',
            'page-size': 'A4',
            'orientation': 'Portrait',
            'dpi': '96',
            'image-dpi': '300',
            'image-quality': '94',
            'title': '',
            'no-outline': '',
            'disable-smart-shrinking': '',
            'disable-javascript': '',
            'load-error-handling': 'ignore',
            'load-media-error-handling': 'ignore',
            'enable-local-file-access': '',
            'images': ''  # Ujistíme se, že obrázky zůstanou zachovány
        }
        
        # Přidáme CSS pro skrytí hlaviček a zápatí přímo do HTML
        hide_elements_css = """
        <style type="text/css">
            @page {
                margin: 0;
                size: A4;
                margin-top: 12mm;
                margin-right: 12mm;
                margin-bottom: 15mm;
                margin-left: 12mm;
            }
            .header, .footer, [class*="header-"], [class*="footer-"] {
                display: none !important;
                height: 0 !important;
                width: 0 !important;
                overflow: hidden !important;
            }
            /* Skrytí názvu souboru */
            .title, [class*="title"], [id*="title"] {
                display: none !important;
            }
        </style>
        """
        
        # Vložíme CSS do HTML obsahu
        if '</head>' in html_for_pdf:
            html_for_pdf = html_for_pdf.replace('</head>', hide_elements_css + '</head>')
        else:
            html_for_pdf = hide_elements_css + html_for_pdf
        try:
            pdf_output.parent.mkdir(parents=True, exist_ok=True)
            # Optional debug dump
            if pdf_dump_path is not None:
                try:
                    pdf_dump_path.parent.mkdir(parents=True, exist_ok=True)
                    pdf_dump_path.write_text(html_for_pdf, encoding='utf-8')
                    print(f"[PDF] Uložen HTML pro debug: {pdf_dump_path}")
                except Exception as e:
                    print(f"[PDF] Nelze uložit debug HTML: {e}")
            # Quick diagnostics: count leftover src/href not data or anchors
            try:
                import re as _chk
                leftovers = _chk.findall(r"\b(src|href|data|xlink:href)\s*=\s*(\"([^\"]+)\"|'([^']+)'|([^\s>]+))", html_for_pdf, flags=_chk.IGNORECASE)
                bad = []
                for _, v1, v2, v3, v4 in leftovers:
                    val = (v2 or v3 or v4 or '').strip().strip('"').strip("'")
                    low = val.lower()
                    if low and not low.startswith(('data:', '#', 'mailto:', 'javascript:')):
                        bad.append(val)
                        if len(bad) >= 5:
                            break
                if bad:
                    print(f"[PDF] Upozornění: Zbývající odkazy v HTML (vzorek): {bad}")
            except Exception:
                pass

            # Write to a temp HTML and convert from file (more robust on Windows)
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix='.html', mode='w', encoding='utf-8') as tf:
                tf.write(html_for_pdf)
                tmp_path = tf.name
            pdfkit.from_file(tmp_path, str(pdf_output), options=options, configuration=config)
            print(f"Vytvořen PDF: {pdf_output}")
        except Exception as e:
            print(f"[PDF] Selhalo generování PDF: {e}")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sestavení knihy z HTML do EPUB a/nebo PDF.")
    parser.add_argument("--input", required=True, help="Vstupní adresář s .html soubory")
    parser.add_argument("--output", required=False, default=None, help="Cílový EPUB soubor (volitelné)")
    parser.add_argument("--sort", choices=["ctime", "name"], default="name", help="Pořadí souborů (ctime|name)")
    parser.add_argument("--title", default=None, help="Název knihy")
    parser.add_argument("--author", default=None, help="Autor")
    parser.add_argument("--lang", default=None, help="Jazyk (např. cs, en, it)")
    parser.add_argument("--css", default=None, help="Cesta k vlastnímu CSS (volitelné)")
    parser.add_argument("--ask-metadata", action="store_true", help="Při startu se interaktivně zeptat na metadata")
    parser.add_argument("--pdf-output", default=None, help="Volitelná cesta pro výstup PDF")
    parser.add_argument("--wkhtmltopdf", default=None, help="Cesta k binárce wkhtmltopdf (volitelné)")
    parser.add_argument("--dump-pdf-html", default=None, help="Ulož generované HTML pro PDF na danou cestu (debug)")
    parser.add_argument("--lang-out", default=None, help="Volitelný cílový jazyk překladu výstupu (např. cs, en, it). Pokud zadán, obsah bude přeložen přes Google Translate (deep-translator).")
    return parser.parse_args(argv)


def main(argv: List[str]) -> None:
    args = parse_args(argv)
    input_dir = Path(args.input)
    epub_output = Path(args.output) if args.output else None
    css_path = Path(args.css) if args.css else None

    if not input_dir.exists() or not input_dir.is_dir():
        print(f"Neplatný vstupní adresář: {input_dir}")
        sys.exit(2)

    # Validate at least one output
    if not args.output and not args.pdf_output:
        print("Musíte zadat alespoň jeden výstup: --output (EPUB) nebo --pdf-output (PDF).")
        sys.exit(2)

    # Metadata defaults if not asked interactively
    title = args.title or "Moje kniha"
    author = args.author or "Unknown"
    lang = args.lang or "cs"

    build_book(
        input_dir=input_dir,
        epub_output=epub_output,
        sort=args.sort,
        title=title,
        author=author,
        lang=lang,
        css_path=css_path,
        ask_metadata=args.ask_metadata,
        pdf_output=Path(args.pdf_output) if args.pdf_output else None,
        wkhtmltopdf_path=Path(args.wkhtmltopdf) if args.wkhtmltopdf else None,
        pdf_dump_path=Path(args.dump_pdf_html) if args.dump_pdf_html else None,
        lang_out=(args.lang_out or None),
    )


if __name__ == "__main__":
    main(sys.argv[1:])
