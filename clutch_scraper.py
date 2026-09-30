"""
=============================================================================
SCRAPER 1: Agencias Digitales de Alto Valor (Clutch.co)
Tecnologia: Playwright Asincrono + BeautifulSoup (Inspector Universal) + Pandas
=============================================================================
- Deteccion automatica de bloqueo Cloudflare con alerta en consola.
- Fallback de navegador: Chrome real -> Chromium empaquetado de Playwright.
- Cascada multi-selector para tarjetas de agencias.
- Debug dump si se detectan 0 tarjetas.
- Flag --only-with-email para control granular de exportacion.
"""

import argparse
import asyncio
import glob
import os
import random
import sys
from typing import Dict, List
from bs4 import BeautifulSoup
import pandas as pd
from playwright.async_api import async_playwright

try:
    from email_crawler import (
        clean_company_name,
        clean_city_string,
        extract_real_website_url,
        extract_emails_from_site,
        validate_lead_quality,
        detect_antibot_block,
        save_debug_dump,
    )
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from email_crawler import (
        clean_company_name,
        clean_city_string,
        extract_real_website_url,
        extract_emails_from_site,
        validate_lead_quality,
        detect_antibot_block,
        save_debug_dump,
    )

DEFAULT_CLUTCH_URL = "https://clutch.co/agencies/digital-marketing"
BROWSER_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clutch_browser_data")


def _fast_unmask_url(url: str) -> str:
    """
    Desofuscacion rapida de URLs de Clutch SIN hacer peticiones HTTP.
    Solo extrae el parametro 'u' de los redirects y limpia UTMs.
    La resolucion HTTP real se hace despues, solo para los leads seleccionados.
    """
    import urllib.parse
    if not url or url == "N/A":
        return ""
    target = url
    if "clutch.co/redirect" in url:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if "u" in qs and qs["u"]:
            target = urllib.parse.unquote(qs["u"][0])
    # Limpiar UTMs
    return target.split("?")[0].rstrip("/")


# ============================================================================
# MOTOR DE NAVEGACION CON RESILIENCIA
# ============================================================================

def _clean_profile_locks(user_data_dir: str):
    """Elimina archivos SingletonLock que bloquean el perfil de Chrome."""
    lock_files = glob.glob(os.path.join(user_data_dir, "SingletonLock"))
    lock_files += glob.glob(os.path.join(user_data_dir, "SingletonCookie"))
    lock_files += glob.glob(os.path.join(user_data_dir, "SingletonSocket"))
    for f in lock_files:
        try:
            os.remove(f)
        except Exception:
            pass


async def launch_browser_context(p, user_data_dir: str):
    """
    Lanza el navegador con resiliencia:
    1. Limpia bloqueos de perfil (SingletonLock).
    2. Intenta con channel='chrome' (Chrome real instalado).
    3. Si falla, hace fallback a Chromium empaquetado de Playwright.
    """
    os.makedirs(user_data_dir, exist_ok=True)
    _clean_profile_locks(user_data_dir)

    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    viewport = {"width": 1280, "height": 800}

    # Intento 1: Chrome real instalado
    try:
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            channel="chrome",
            headless=False,
            args=browser_args,
            viewport=viewport
        )
        print("[OK] Navegador lanzado: Google Chrome (canal real)")
        return context
    except Exception as e:
        print(f"[!] Chrome no disponible ({e}). Intentando Chromium de Playwright...")

    # Intento 2: Chromium empaquetado de Playwright (fallback)
    try:
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            args=browser_args,
            viewport=viewport
        )
        print("[OK] Navegador lanzado: Chromium (Playwright)")
        return context
    except Exception as e2:
        print(f"[FATAL] No se pudo lanzar ningun navegador: {e2}")
        raise


async def navigate_with_antibot_check(page, url: str, max_cf_wait: int = 15) -> str:
    """
    Navega a la URL, detecta bloqueos Cloudflare y espera para que se resuelvan.
    Retorna el HTML de la pagina cargada.
    """
    print(f"[*] Navegando a: {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"[!] Error al cargar URL: {e}")

    # Esperar render inicial
    await asyncio.sleep(random.uniform(2.0, 3.5))

    # Chequeo de bloqueo anti-bot
    title = await page.title()
    html_head = await page.content()

    if detect_antibot_block(title, html_head[:3000]):
        print(f"[!] ⚠️  BLOQUEO ANTI-BOT DETECTADO en: {url}")
        print(f"[!] Titulo de pagina: '{title}'")
        print(f"[!] Esperando {max_cf_wait}s para resolucion automatica o intervencion manual...")
        await asyncio.sleep(max_cf_wait)

        # Re-chequear despues de la espera
        title = await page.title()
        html_head = await page.content()
        if detect_antibot_block(title, html_head[:3000]):
            print("[!] ❌ El bloqueo persiste tras la espera. El HTML puede estar incompleto.")
        else:
            print("[OK] ✅ Bloqueo resuelto. Continuando extraccion.")

    print(f"[*] Titulo de pagina: '{title}'")
    return await page.content()


# ============================================================================
# PARSER DE TARJETAS CLUTCH (MULTI-SELECTOR)
# ============================================================================

# Cascada de selectores para tarjetas de agencias
CLUTCH_CARD_SELECTORS = [
    ".provider-row",
    "li.provider-row",
    "div[data-provider-id]",
    "li[data-provider-id]",
    "article.provider",
    ".directory-list li",
    ".providers-list li",
]


def parse_clutch_agencies(html: str) -> List[Dict[str, str]]:
    """
    Parsea el HTML universal de Clutch con BeautifulSoup.
    Usa cascada de selectores para maxima resiliencia.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Intentar multiples selectores en cascada
    cards = []
    for selector in CLUTCH_CARD_SELECTORS:
        cards = soup.select(selector)
        if cards:
            break

    agencies: List[Dict[str, str]] = []
    seen_names = set()

    for card in cards:
        # 1. Nombre de la empresa (cascada de selectores)
        name_el = card.select_one(
            "h3.company_info a, a.company_title, .company_info a, "
            ".provider__title a, h3 a, h2 a, "
            "a[data-link_text='Profile Name']"
        )
        raw_name = name_el.get_text(strip=True) if name_el else ""
        clean_name = clean_company_name(raw_name)

        if not clean_name or clean_name.lower() in seen_names:
            continue

        # 2. Ciudad / Ubicacion
        loc_el = card.select_one(
            ".locality, .location, .provider-detail__item--location, "
            ".provider__highlights-item--location, "
            "span[data-content*='Location']"
        )
        raw_loc = loc_el.get_text(strip=True) if loc_el else ""
        city = clean_city_string(raw_loc)

        # 3. Presupuesto minimo (Min_Project_Size)
        min_proj_el = card.select_one(
            ".min-project-size, [class*='min-project-size'], "
            ".provider__highlights-item.sg-tooltip-v2, "
            "span[data-content*='Min. project size'], div.list-item"
        )
        min_project_size = "N/A"
        if min_proj_el:
            txt = min_proj_el.get_text(strip=True)
            if "$" in txt or "Undisclosed" in txt:
                min_project_size = txt

        if min_project_size == "N/A":
            highlights = card.select(
                ".provider__highlights-item, .list-item, div.sg-tooltip-v2"
            )
            for h in highlights:
                txt = h.get_text(strip=True)
                if ("$" in txt and ("+" in txt or "<" in txt or "," in txt)) or "Undisclosed" in txt:
                    min_project_size = txt
                    break

        # 4. Enlace al sitio web oficial
        web_el = card.select_one(
            "a.website-link__item, a.visit-website, "
            "a[data-link_text='website'], a[data-link_text='Visit Website'], "
            "a[href*='clutch.co/redirect'], a[href*='visit_website'], "
            "li.website-link a"
        )
        raw_web = web_el.get("href") if web_el else ""
        # Desofuscacion rapida sin HTTP (la resolucion HTTP se hace en la fase de emails)
        clean_web = _fast_unmask_url(raw_web)

        seen_names.add(clean_name.lower())
        agencies.append({
            "Business_Name": clean_name,
            "City": city if city else "N/A",
            "Min_Project_Size": min_project_size,
            "Website": clean_web if clean_web else "N/A",
            "Email": ""
        })

    return agencies


# ============================================================================
# ORQUESTADOR PRINCIPAL
# ============================================================================

async def run_clutch_scraper(
    target_url: str = DEFAULT_CLUTCH_URL,
    max_leads: int = 15,
    crawl_emails: bool = True,
    only_with_email: bool = False,
    output_path: str = "clutch_agencies_leads.xlsx"
):
    print("=" * 65)
    print(" [*] INICIANDO SCRAPER 1: CLUTCH.CO (Agencias Digitales)")
    print(f" [*] URL Objetivo: {target_url}")
    print(f" [*] Leads solicitados: {max_leads}")
    print(f" [*] Solo con email: {'SI' if only_with_email else 'NO (exportar todos)'}")
    print("=" * 65)

    all_agencies: List[Dict[str, str]] = []
    current_page = 0

    async with async_playwright() as p:
        context = await launch_browser_context(p, BROWSER_PROFILE_DIR)
        page = context.pages[0] if context.pages else await context.new_page()

        while len(all_agencies) < max_leads:
            separator = "&" if "?" in target_url else "?"
            page_url = f"{target_url}{separator}page={current_page}" if current_page > 0 else target_url

            print(f"\n--> [Página {current_page + 1}]")

            # Navegar con deteccion anti-bot
            html = await navigate_with_antibot_check(page, page_url)

            # Esperar selector de tarjetas
            try:
                await page.wait_for_selector(
                    ", ".join(CLUTCH_CARD_SELECTORS), timeout=15000
                )
            except Exception:
                print("[!] Timeout esperando selector de tarjetas.")

            await asyncio.sleep(random.uniform(1.0, 2.0))
            html = await page.content()

            # Parsear tarjetas
            extracted = parse_clutch_agencies(html)

            if not extracted:
                print(f"[!] 0 tarjetas encontradas en pagina {current_page + 1}.")
                save_debug_dump(html, f"debug_clutch_page_{current_page + 1}.html")
                break

            # Filtrar con validate_lead_quality
            valid = [a for a in extracted if validate_lead_quality(a)]
            print(f"[+] {len(extracted)} tarjetas detectadas, {len(valid)} pasaron filtro de calidad.")

            for item in valid:
                all_agencies.append(item)
                if len(all_agencies) >= max_leads:
                    break

            current_page += 1
            await asyncio.sleep(random.uniform(2.0, 3.5))

        await context.close()

    print(f"\n[OK] Total de agencias recolectadas: {len(all_agencies)}")

    # Fase 2: Crawler de Emails
    if crawl_emails:
        print("\n" + "-" * 65)
        print(" [*] INICIANDO CRAWLER DE EMAILS B2B...")
        print("-" * 65)
        for idx, agency in enumerate(all_agencies, 1):
            web = agency.get("Website", "")
            if web and web != "N/A" and web.startswith("http"):
                print(f"[{idx}/{len(all_agencies)}] Explorando: {web}...")
                email = extract_emails_from_site(web, timeout=6)
                if email:
                    print(f"    --> [EMAIL]: {email}")
                    agency["Email"] = email
                else:
                    print("    --> Sin email publico.")
            else:
                print(f"[{idx}/{len(all_agencies)}] Sin URL de website valida.")

    # Fase 3: Exportar a Excel
    df = pd.DataFrame(all_agencies)
    cols = ["Business_Name", "City", "Min_Project_Size", "Website", "Email"]
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols]

    # Filtrado condicional: --only-with-email descarta leads sin correo
    if only_with_email:
        df.replace("", pd.NA, inplace=True)
        df.dropna(subset=["Email"], inplace=True)
        df.fillna("N/A", inplace=True)
        print(f"[*] Filtro --only-with-email aplicado. Filas restantes: {len(df)}")
    else:
        df.replace("", "N/A", inplace=True)

    df.to_excel(output_path, index=False, engine="openpyxl")
    print("\n" + "=" * 65)
    print(f"[EXITO] Archivo generado: {output_path}")
    print(f"Total registros exportados: {len(df)}")
    print("=" * 65)
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scraper de Agencias Clutch.co con Playwright y BeautifulSoup"
    )
    parser.add_argument("--url", default=DEFAULT_CLUTCH_URL, help="URL de categoria en Clutch.co")
    parser.add_argument("--target", type=int, default=15, help="Numero maximo de leads a extraer")
    parser.add_argument("--output", default="clutch_agencies_leads.xlsx", help="Ruta del archivo Excel de salida")
    parser.add_argument("--no-emails", action="store_true", help="Desactivar crawler de emails")
    parser.add_argument("--only-with-email", action="store_true", help="Exportar solo leads que tengan email")

    args = parser.parse_args()
    asyncio.run(run_clutch_scraper(
        target_url=args.url,
        max_leads=args.target,
        crawl_emails=not args.no_emails,
        only_with_email=args.only_with_email,
        output_path=args.output
    ))
