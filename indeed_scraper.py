"""
=============================================================================
SCRAPER 2: Empresas buscando trabajo manual en Indeed.com ("Desperation Hack")
Tecnologia: Playwright Asincrono + BeautifulSoup (Inspector Universal) + Pandas
=============================================================================
- Deteccion automatica de bloqueo Cloudflare con alerta en consola.
- Fallback de navegador: Chrome real -> Chromium empaquetado de Playwright.
- Cascada multi-selector para tarjetas de empleo.
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
import urllib.parse
from bs4 import BeautifulSoup
import pandas as pd
from playwright.async_api import async_playwright

try:
    from email_crawler import (
        clean_company_name,
        clean_city_string,
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
        extract_emails_from_site,
        validate_lead_quality,
        detect_antibot_block,
        save_debug_dump,
    )

DEFAULT_INDEED_URL = "https://www.indeed.com/jobs?q=data+entry+OR+excel+OR+spreadsheet&l=Florida"
INDEED_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "indeed_browser_data")


# ============================================================================
# MOTOR DE NAVEGACION CON RESILIENCIA (Compartido con clutch_scraper)
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
    1. Limpia bloqueos de perfil.
    2. Chrome real -> fallback Chromium.
    """
    os.makedirs(user_data_dir, exist_ok=True)
    _clean_profile_locks(user_data_dir)

    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    viewport = {"width": 1280, "height": 800}

    try:
        context = await p.chromium.launch_persistent_context(
            user_data_dir, channel="chrome", headless=False,
            args=browser_args, viewport=viewport
        )
        print("[OK] Navegador lanzado: Google Chrome (canal real)")
        return context
    except Exception as e:
        print(f"[!] Chrome no disponible ({e}). Intentando Chromium...")

    try:
        context = await p.chromium.launch_persistent_context(
            user_data_dir, headless=False,
            args=browser_args, viewport=viewport
        )
        print("[OK] Navegador lanzado: Chromium (Playwright)")
        return context
    except Exception as e2:
        print(f"[FATAL] No se pudo lanzar ningun navegador: {e2}")
        raise


async def navigate_with_antibot_check(page, url: str, max_cf_wait: int = 15) -> str:
    """
    Navega a la URL, detecta bloqueos Cloudflare y espera resolucion.
    """
    print(f"[*] Navegando a: {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"[!] Error al cargar URL: {e}")

    await asyncio.sleep(random.uniform(2.0, 3.5))

    title = await page.title()
    html_head = await page.content()

    if detect_antibot_block(title, html_head[:3000]):
        print(f"[!] ⚠️  BLOQUEO ANTI-BOT DETECTADO en: {url}")
        print(f"[!] Titulo: '{title}'")
        print(f"[!] Esperando {max_cf_wait}s para resolucion...")
        await asyncio.sleep(max_cf_wait)

        title = await page.title()
        html_head = await page.content()
        if detect_antibot_block(title, html_head[:3000]):
            print("[!] ❌ El bloqueo persiste. El HTML puede estar incompleto.")
        else:
            print("[OK] ✅ Bloqueo resuelto. Continuando extraccion.")

    print(f"[*] Titulo de pagina: '{title}'")
    return await page.content()


# ============================================================================
# PARSER DE TARJETAS INDEED (MULTI-SELECTOR)
# ============================================================================

INDEED_CARD_SELECTORS = [
    ".job_seen_beacon",
    "div[class*='job_seen_beacon']",
    "td.resultContent",
    "div[data-jk]",
    ".jobCard_mainContent",
    "li.css-5lfssm",
    "div.slider_item",
]


def parse_indeed_jobs(html: str) -> List[Dict[str, str]]:
    """
    Parsea las tarjetas de empleo de Indeed con BeautifulSoup.
    Usa cascada de selectores para maxima resiliencia.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Intentar multiples selectores en cascada
    cards = []
    used_selector = ""
    for selector in INDEED_CARD_SELECTORS:
        cards = soup.select(selector)
        if cards:
            used_selector = selector
            break

    if used_selector:
        print(f"[DEBUG] Selector activo: '{used_selector}' -> {len(cards)} tarjetas")

    jobs: List[Dict[str, str]] = []
    seen_keys = set()

    for card in cards:
        # 1. Titulo del puesto
        title_el = card.select_one(
            "h2.jobTitle span[title], h2.jobTitle a, "
            "h2.jobTitle span, a[data-jk], "
            "h2[id^='jobTitle'] span"
        )
        job_title = title_el.get_text(strip=True) if title_el else ""
        if not job_title or job_title.lower() == "new":
            parent = card.select_one("h2.jobTitle, h2[id^='jobTitle']")
            if parent:
                job_title = parent.get_text(strip=True).replace("new", "").strip()

        # 2. Nombre de la empresa
        comp_el = card.select_one(
            "span[data-testid='company-name'], .companyName, "
            "[data-testid='company-name'], [data-company-name]"
        )
        raw_company = comp_el.get_text(strip=True) if comp_el else ""
        clean_company = clean_company_name(raw_company)

        if not clean_company:
            continue

        # 3. Ciudad aislada
        loc_el = card.select_one(
            "div[data-testid='text-location'], .companyLocation, "
            "[data-testid='myJobsStateDate']"
        )
        raw_loc = loc_el.get_text(strip=True) if loc_el else ""
        city = clean_city_string(raw_loc)

        # 4. Enlace directo a la oferta
        link_el = card.select_one("h2.jobTitle a, a[data-jk], a.jcs-JobTitle")
        job_url = ""
        if link_el:
            href = link_el.get("href", "")
            if href:
                if href.startswith("http"):
                    job_url = href
                else:
                    job_url = urllib.parse.urljoin("https://www.indeed.com", href)

        # 5. Enlace al sitio web de la empresa
        comp_link_el = card.select_one(
            "a[data-testid='company-name'], a.companyOverviewLink"
        )
        website = ""
        if comp_link_el:
            cmp_href = comp_link_el.get("href", "")
            if cmp_href:
                if cmp_href.startswith("http") and "indeed.com" not in cmp_href:
                    website = cmp_href
                else:
                    website = urllib.parse.urljoin("https://www.indeed.com", cmp_href)

        unique_key = f"{clean_company.lower()}_{job_title.lower()}"
        if unique_key in seen_keys:
            continue

        seen_keys.add(unique_key)
        jobs.append({
            "Business_Name": clean_company,
            "City": city if city else "Florida",
            "Job_Title": job_title,
            "Job_URL": job_url,
            "Website": website if website else "N/A",
            "Email": ""
        })

    return jobs


# ============================================================================
# ORQUESTADOR PRINCIPAL
# ============================================================================

async def run_indeed_scraper(
    target_url: str = DEFAULT_INDEED_URL,
    max_leads: int = 15,
    crawl_emails: bool = True,
    only_with_email: bool = False,
    output_path: str = "indeed_hiring_leads.xlsx"
):
    print("=" * 65)
    print(" [*] INICIANDO SCRAPER 2: INDEED.COM (Desperation Hack)")
    print(f" [*] URL Objetivo: {target_url}")
    print(f" [*] Leads solicitados: {max_leads}")
    print(f" [*] Solo con email: {'SI' if only_with_email else 'NO (exportar todos)'}")
    print("=" * 65)

    all_jobs: List[Dict[str, str]] = []
    start_index = 0

    async with async_playwright() as p:
        context = await launch_browser_context(p, INDEED_PROFILE_DIR)
        page = context.pages[0] if context.pages else await context.new_page()

        while len(all_jobs) < max_leads:
            separator = "&" if "?" in target_url else "?"
            page_url = f"{target_url}{separator}start={start_index}" if start_index > 0 else target_url

            print(f"\n--> [Offset start={start_index}]")

            # Navegar con deteccion anti-bot
            html = await navigate_with_antibot_check(page, page_url)

            # Esperar selector de tarjetas
            try:
                await page.wait_for_selector(
                    ", ".join(INDEED_CARD_SELECTORS), timeout=15000
                )
            except Exception:
                print("[!] Timeout esperando selector de tarjetas de empleo.")

            await asyncio.sleep(random.uniform(1.0, 2.0))
            html = await page.content()

            # Parsear tarjetas
            extracted = parse_indeed_jobs(html)

            if not extracted:
                print(f"[!] 0 tarjetas encontradas en offset {start_index}.")
                save_debug_dump(html, f"debug_indeed_offset_{start_index}.html")
                break

            # Filtrar con validate_lead_quality
            valid = [j for j in extracted if validate_lead_quality(j)]
            print(f"[+] {len(extracted)} tarjetas detectadas, {len(valid)} pasaron filtro de calidad.")

            for item in valid:
                all_jobs.append(item)
                if len(all_jobs) >= max_leads:
                    break

            start_index += 10
            await asyncio.sleep(random.uniform(2.0, 3.5))

        await context.close()

    print(f"\n[OK] Total de empleos identificados: {len(all_jobs)}")

    # Fase 2: Crawler de emails
    if crawl_emails:
        print("\n" + "-" * 65)
        print(" [*] PROCESANDO CRAWLER DE EMAILS PARA EMPRESAS...")
        print("-" * 65)
        for idx, job in enumerate(all_jobs, 1):
            web = job.get("Website", "")
            if web and web != "N/A" and web.startswith("http") and "indeed.com" not in web:
                print(f"[{idx}/{len(all_jobs)}] Explorando: {web}...")
                email = extract_emails_from_site(web, timeout=6)
                if email:
                    print(f"    --> [EMAIL]: {email}")
                    job["Email"] = email
            else:
                job["Email"] = ""

    # Fase 3: Exportar a Excel
    df = pd.DataFrame(all_jobs)
    cols = ["Business_Name", "City", "Job_Title", "Job_URL", "Website", "Email"]
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols]

    # Filtrado condicional
    if only_with_email:
        df.replace("", pd.NA, inplace=True)
        df.dropna(subset=["Email"], inplace=True)
        df.fillna("N/A", inplace=True)
        print(f"[*] Filtro --only-with-email aplicado. Filas restantes: {len(df)}")
    else:
        # Filtrar solo por datos esenciales (empresa y puesto deben existir)
        df.replace("", pd.NA, inplace=True)
        df.dropna(subset=["Business_Name", "Job_Title"], inplace=True)
        df.fillna("N/A", inplace=True)

    df.to_excel(output_path, index=False, engine="openpyxl")
    print("\n" + "=" * 65)
    print(f"[EXITO] Archivo generado: {output_path}")
    print(f"Total registros exportados: {len(df)}")
    print("=" * 65)
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scraper de Empleos Indeed con Playwright y BeautifulSoup"
    )
    parser.add_argument("--url", default=DEFAULT_INDEED_URL, help="URL de busqueda en Indeed.com")
    parser.add_argument("--target", type=int, default=15, help="Numero maximo de leads a extraer")
    parser.add_argument("--output", default="indeed_hiring_leads.xlsx", help="Ruta del archivo Excel de salida")
    parser.add_argument("--no-emails", action="store_true", help="Desactivar crawler de emails")
    parser.add_argument("--only-with-email", action="store_true", help="Exportar solo leads que tengan email")

    args = parser.parse_args()
    asyncio.run(run_indeed_scraper(
        target_url=args.url,
        max_leads=args.target,
        crawl_emails=not args.no_emails,
        only_with_email=args.only_with_email,
        output_path=args.output
    ))
