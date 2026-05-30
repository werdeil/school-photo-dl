"""Téléchargeur de photos depuis toutemonannee.com.

Selenium est utilisé uniquement pour le login (formulaire en 2 étapes) et pour
lister les articles d'un espace. Les photos elles-mêmes sont récupérées via
l'API JSON `/journal/{space}/posts/photos/{post}` en HTTP direct.
"""

import os
import re
import time
import logging
from collections import namedtuple
from datetime import timedelta

import requests
from dotenv import load_dotenv
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

from school_photo_dl.shared.driver import init_driver
from school_photo_dl.shared.utils import (
    build_name_prefix,
    configure_logging,
    parse_french_date,
    safe_name,
    set_image_datetime,
    slugify,
)

logger = logging.getLogger(__name__)

BASE_TMA_URL = 'https://www.toutemonannee.com'
DASHBOARD_URL = f'{BASE_TMA_URL}/dashboard'


# ---------------------------------------------------------------------------
# Authentification (Selenium)
# ---------------------------------------------------------------------------

def login_with_credentials(driver, username, password):
    """Remplit le formulaire de login en 2 étapes et retourne le cookie diedm_session."""
    driver.get(f'{BASE_TMA_URL}/login')
    time.sleep(4)

    driver.find_element(By.NAME, 'username').send_keys(username)
    driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
    time.sleep(3)

    driver.find_element(By.NAME, 'password').send_keys(password)
    driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
    time.sleep(5)

    cookies = {c['name']: c['value'] for c in driver.get_cookies()}
    session_cookie = cookies.get('diedm_session')
    if not session_cookie or '/connect' in driver.current_url or '/login' in driver.current_url:
        raise ValueError("Login échoué : vérifiez TMA_USERNAME et TMA_PASSWORD.")

    logger.info("Connexion réussie, cookie de session récupéré.")
    return session_cookie


def get_session_cookie(driver):
    """Retourne le cookie diedm_session via login Selenium."""
    username = os.getenv('TMA_USERNAME')
    password = os.getenv('TMA_PASSWORD')
    if not username or not password:
        raise ValueError(
            "Définissez TMA_USERNAME et TMA_PASSWORD dans le .env."
        )
    return login_with_credentials(driver, username, password)


# ---------------------------------------------------------------------------
# Listing : espaces (HTTP API) + articles (DOM Selenium)
# ---------------------------------------------------------------------------

def get_spaces(session_cookie):
    """Récupère la liste des espaces/albums via l'API."""
    list_response = requests.get(
        f'{BASE_TMA_URL}/spaces/list',
        cookies={'diedm_session': session_cookie},
        timeout=10
    )
    data = list_response.json()
    spaces = []
    for space in data['spaces']:
        logger.info("UUID: %s, Année : %s, Nom : %s",
                    space['uuid'], space['display_years'], space['display_name'])
        spaces.append({
            'name': space['display_name'],
            'uuid': space['uuid'],
            'years': space.get('display_years', ''),
        })
    for space in data.get('spaces_soon_archived', []):
        logger.info("UUID: %s, Année : %s, Nom (archivé) : %s",
                    space['uuid'], space['display_years'], space['display_name'])
        spaces.append({
            'name': space['display_name'],
            'uuid': space['uuid'],
            'years': space.get('display_years', ''),
        })
    return spaces


def scroll_to_load_all_articles(driver):
    """Scrolle jusqu'en bas pour déclencher le chargement paresseux de tous les articles."""
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(3)
    articles = driver.find_elements(By.CSS_SELECTOR, "article:has(button.gallery-trigger)")
    prev_count = 0
    while len(articles) > prev_count:
        prev_count = len(articles)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        articles = driver.find_elements(By.CSS_SELECTOR, "article:has(button.gallery-trigger)")
        logger.debug("Articles après scroll : %d", len(articles))
    return articles


def collect_article_data(driver):
    """Collecte (date, title, post_url) de tous les articles sans naviguer hors de la page."""
    articles = scroll_to_load_all_articles(driver)
    logger.info("Nombre total d'articles : %d", len(articles))
    result = []
    for article in articles:
        try:
            date = (article.find_element(By.CSS_SELECTOR, "div.day").text + " "
                    + article.find_element(By.CSS_SELECTOR, "div.month").text)
        except NoSuchElementException:
            date = "unknown"
        try:
            link = article.find_element(By.XPATH, './/header//a[contains(@href,"/posts/")]')
            post_url = link.get_attribute('href')
            try:
                title_text = link.find_element(By.TAG_NAME, "span").text
            except NoSuchElementException:
                title_text = ""
        except NoSuchElementException:
            logger.warning("Article sans lien de post, ignoré.")
            continue
        result.append((date, title_text, post_url))
    return result


# ---------------------------------------------------------------------------
# API photos d'un post + téléchargement
# ---------------------------------------------------------------------------

_POST_ID_RE = re.compile(r'/posts/(\d+)')


def _parse_post_id(post_url):
    """Extrait l'ID numérique d'un post depuis l'URL `/posts/{id}`."""
    match = _POST_ID_RE.search(post_url or '')
    return match.group(1) if match else None


def fetch_post_photos(space_uuid, post_id, session_cookie):
    """Récupère toutes les photos d'un post via l'API JSON paginée.

    L'endpoint `/journal/{space}/posts/photos/{post}?page=N&per_page=K`
    renvoie `{total, current_page, last_page, page_size, data: [...]}`
    où chaque élément de `data` expose `src` (URL HD), `extension`, `uuid`, etc.
    """
    cookies = {"diedm_session": session_cookie}
    photos = []
    page = 1
    while True:
        url = (f"{BASE_TMA_URL}/journal/{space_uuid}/posts/photos/{post_id}"
               f"?page={page}&per_page=100")
        resp = requests.get(url, cookies=cookies, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("data") or []
        photos.extend(batch)
        last_page = data.get("last_page", 1)
        if page >= last_page or not batch:
            break
        page += 1
    return photos


def download_image(hd_img_url, dest_path, session_cookie=None):
    """Télécharge une image HD vers dest_path. Retourne le chemin ou None."""
    img_name = os.path.basename(dest_path)
    if os.path.exists(dest_path):
        logger.debug("Déjà téléchargée : %s", img_name)
        return dest_path
    clean_img_url = re.sub(r'\?.*$', '', hd_img_url)
    try:
        cookies = {'diedm_session': session_cookie} if session_cookie else {}
        img_data = requests.get(clean_img_url, cookies=cookies, timeout=30).content
        with open(dest_path, 'wb') as img_file:
            img_file.write(img_data)
        logger.info("Image sauvegardée : %s", img_name)
        return dest_path
    except Exception as err:  # pylint: disable=broad-except
        logger.error("Erreur téléchargement %s : %s", clean_img_url, err)
        return None


def _apply_photo_date(img_path, base_dt, index):
    """Applique base_dt + index minutes à l'image si base_dt est défini."""
    if base_dt is None or img_path is None:
        return
    set_image_datetime(img_path, base_dt + timedelta(minutes=index))


# ---------------------------------------------------------------------------
# Orchestration par post / espace
# ---------------------------------------------------------------------------

_PostCtx = namedtuple('_PostCtx', 'folder session_cookie base_dt name_prefix')


def _download_api_photo(photo, index, ctx):
    """Télécharge une photo retournée par l'API et applique son EXIF date."""
    src = photo.get("src")
    if not src:
        return
    ext = (photo.get("extension") or "jpg").lstrip(".").lower()
    num = f"{index + 1:03d}"
    name = f"{num}_{ctx.name_prefix}.{ext}" if ctx.name_prefix else f"{num}.{ext}"
    dest_path = os.path.join(ctx.folder, name)
    img_path = download_image(src, dest_path, ctx.session_cookie)
    _apply_photo_date(img_path, ctx.base_dt, index)


def _build_post_naming(date, title_text, base_dt):
    """Retourne (folder_name, name_prefix) pour un post.

    Dossier : `YYYY-MM-DD - titre` si la date est parsable, sinon `date FR - titre`.
    Préfixe fichier : `YYYY-MM-DD_slug` avec dégradés propres si l'un manque.
    """
    iso_date = base_dt.strftime("%Y-%m-%d") if base_dt else ""
    folder_date = iso_date or date
    folder_label = f"{folder_date} - {title_text}" if title_text else folder_date
    folder_name = safe_name(folder_label)
    name_prefix = build_name_prefix(iso_date, slugify(title_text))
    return folder_name, name_prefix


def _prepare_post_ctx(article_data, save_folder_path, session_cookie, space):
    """Calcule le dossier et le contexte de téléchargement pour un post."""
    date, title_text, post_url = article_data
    base_dt = parse_french_date(date, space.get('years', ''))
    folder_name, name_prefix = _build_post_naming(date, title_text, base_dt)
    folder = os.path.join(save_folder_path, folder_name)
    os.makedirs(folder, exist_ok=True)
    ctx = _PostCtx(folder, session_cookie, base_dt, name_prefix)
    return ctx, title_text, post_url


def process_post(article_data, save_folder_path, session_cookie, space):
    """Télécharge toutes les photos d'un post via l'API."""
    ctx, title_text, post_url = _prepare_post_ctx(
        article_data, save_folder_path, session_cookie, space,
    )
    logger.info("Traitement du post : %s", title_text or post_url)

    post_id = _parse_post_id(post_url)
    if not post_id:
        logger.warning("Aucun post_id extrait de %s, ignoré.", post_url)
        return

    try:
        photos = fetch_post_photos(space['uuid'], post_id, ctx.session_cookie)
    except (requests.RequestException, ValueError) as exc:
        logger.error("Échec API photos pour post %s (%s) ; aucune image récupérée.",
                     post_id, exc)
        return

    logger.info("%d photo(s) pour : %s", len(photos), title_text)
    for index, photo in enumerate(photos):
        _download_api_photo(photo, index, ctx)


def process_space(driver, space, base_download_dir, session_cookie):
    """Traite un espace/album : collecte les articles et les télécharge."""
    url = f"{BASE_TMA_URL}/journal/{space['uuid']}"
    logger.info("Traitement de l'espace : %s — %s", space['name'], url)
    save_folder_path = os.path.join(base_download_dir, space['name'])
    driver.add_cookie({'name': f'noShowAlbumPopupAnymore_{space["uuid"]}', 'value': '1'})
    driver.add_cookie({'name': 'noShowSouvenirPopupAnymore', 'value': '1'})
    driver.get(url)
    time.sleep(5)

    for article_data in collect_article_data(driver):
        process_post(article_data, save_folder_path, session_cookie, space)


def main():
    """Point d'entrée principal : initialise le driver et traite tous les espaces."""
    load_dotenv()
    configure_logging()

    download_dir = os.getenv('DOWNLOAD_DIR')
    if not download_dir:
        raise EnvironmentError("DOWNLOAD_DIR is not set in .env")
    base_download_dir = os.path.expanduser(download_dir)

    headless = os.getenv('HEADLESS', 'true').lower() != 'false'
    driver = init_driver(headless=headless)
    try:
        driver.get(DASHBOARD_URL)
        session_cookie = get_session_cookie(driver)
        driver.add_cookie({'name': 'diedm_session', 'value': session_cookie})
        driver.get(DASHBOARD_URL)
        time.sleep(5)
        for space in get_spaces(session_cookie):
            process_space(driver, space, base_download_dir, session_cookie)
    finally:
        driver.quit()
    logger.info("Terminé. Images dans : %s", base_download_dir)


if __name__ == "__main__":
    main()
