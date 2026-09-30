"""
=============================================================================
SCRAPER 2: Empresas buscando trabajo manual en Indeed.com ("Desperation Hack")
Tecnologia: Playwright Asincrono + BeautifulSoup (Inspector Universal) + Pandas
=============================================================================
Detecta empresas que están contratando para trabajo manual (data entry, excel, etc.),
indicador clave de operaciones sin automatizar ("Desperation Hack").
Extrae el HTML universal con `page.content()`, lo procesa con BeautifulSoup y exporta
leads limpios listos para secuencias de Cold Email B2B.
"""

import argparse
import asyncio
import os
import random
import sys
from typing import Dict, List
import urllib.parse
from bs4 import BeautifulSoup
import pandas as pd
from playwright.async_api import async_playwright

# Importar funciones de limpieza y crawler de emails
try:
    from email_crawler import (
        clean_company_name,
        clean_city_string,
        extract_emails_from_site,
    )
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from email_crawler import (
        clean_company_name,
        clean_city_string,
        extract_emails_from_site,
    )

DEFAULT_INDEED_URL = "https://www.indeed.com/jobs?q=data+entry+OR+excel+OR+spreadsheet&l=Florida"
INDEED_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "indeed_browser_data")


async def fetch_indeed_html(url: str, user_data_dir: str) -> str:
    """
    Carga la pagina de Indeed utilizando Playwright con navegador Chrome autentico
    y contexto persistente para sobrepasar retos de Cloudflare.
    """
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir,
            channel="chrome",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ],
            viewport={"width": 1280, "height": 800}
        )
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        await asyncio.sleep(random.uniform(0.5, 1.2))
        print(f"[*] Navegando a Indeed: {url}")
        
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[!] Error al cargar URL en Indeed: {e}")

        # Retardo humano para completar hidratacion del DOM
        await asyncio.sleep(random.uniform(2.5, 4.0))

        # Extraer HTML completo (Inspector Universal)
        html_content = await page.content()
        await context.close()
        return html_content


def parse_indeed_jobs(html: str) -> List[Dict[str, str]]:
    """
    Parsea las tarjetas de empleo de Indeed (.job_seen_beacon / .resultContent)
    con BeautifulSoup y extrae variables exactas.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Tarjetas de ofertas de Indeed
    cards = soup.select(
        ".job_seen_beacon, div[class*='job_seen_beacon'], "
        "td.resultContent, .jobCard_mainContent"
    )

    jobs: List[Dict[str, str]] = []
    seen_keys = set()

    for card in cards:
        # 1. Titulo del puesto (Job_Title)
        title_el = card.select_one(
            "h2.jobTitle span[title], h2.jobTitle a, "
            "h2.jobTitle span, a[data-jk]"
        )
        job_title = title_el.get_text(strip=True) if title_el else ""
        if not job_title or job_title.lower() == "new":
            # Si el span tomo la etiqueta 'new', buscar elemento hermano o padre
            parent = card.select_one("h2.jobTitle")
            if parent:
                job_title = parent.get_text(strip=True).replace("new", "").strip()

        # 2. Nombre de la empresa (Business_Name)
        comp_el = card.select_one(
            "span[data-testid='company-name'], .companyName, "
            "[data-testid='company-name']"
        )
        raw_company = comp_el.get_text(strip=True) if comp_el else ""
        clean_company = clean_company_name(raw_company)
        
        if not clean_company or clean_company.lower() in ["indeed", "confidential"]:
            continue

        # 3. Ciudad aislada (City)
        loc_el = card.select_one("div[data-testid='text-location'], .companyLocation")
        raw_loc = loc_el.get_text(strip=True) if loc_el else ""
        city = clean_city_string(raw_loc)

        # 4. Enlace directo a la oferta (Job_URL)
        link_el = card.select_one("h2.jobTitle a, a[data-jk], a.jcs-JobTitle")
        job_url = ""
        if link_el:
            href = link_el.get("href", "")
            if href:
                if href.startswith("http"):
                    job_url = href
                else:
                    job_url = urllib.parse.urljoin("https://www.indeed.com", href)

        # 5. Enlace al sitio web de la empresa (si está referenciado en la tarjeta)
        comp_link_el = card.select_one("a[data-testid='company-name'], a.companyOverviewLink")
        website = ""
        if comp_link_el:
            cmp_href = comp_link_el.get("href", "")
            if cmp_href:
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
            "Email": ""  # Se completara con el crawler si hay web externa
        })

    return jobs


async def parse_and_extract_jobs(page) -> List[Dict[str, str]]:
    """
    Espera a que aparezcan las tarjetas de empleo en el DOM y extrae el HTML universal.
    """
    try:
        await page.wait_for_selector(
            ".job_seen_beacon, div[class*='job_seen_beacon'], td.resultContent, .jobCard_mainContent",
            timeout=15000
        )
    except Exception:
        print("[!] Timeout esperando selector de tarjetas de empleo. Procediendo con el HTML disponible...")

    await asyncio.sleep(random.uniform(1.5, 2.5))
    html = await page.content()
    return parse_indeed_jobs(html)


async def run_indeed_scraper(
    target_url: str = DEFAULT_INDEED_URL,
    max_leads: int = 15,
    crawl_emails: bool = True,
    output_path: str = "indeed_hiring_leads.xlsx"
):
    """
    Orquesta la extraccion de empresas contratando trabajo manual en Indeed.com
    y exporta los resultados limpios a Excel.
    """
    print("=" * 65)
    print(" [*] INICIANDO SCRAPER 2: INDEED.COM (Desperation Hack)")
    print(f" [*] URL Objetivo: {target_url}")
    print(f" [*] Leads solicitados: {max_leads}")
    print("=" * 65)

    all_jobs: List[Dict[str, str]] = []
    start_index = 0

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            INDEED_PROFILE_DIR,
            channel="chrome",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ],
            viewport={"width": 1280, "height": 800}
        )
        page = context.pages[0] if context.pages else await context.new_page()

        while len(all_jobs) < max_leads:
            # Paginacion en Indeed: &start=0, &start=10, &start=20...
            separator = "&" if "?" in target_url else "?"
            page_url = f"{target_url}{separator}start={start_index}" if start_index > 0 else target_url

            print(f"\n--> [Offset start={start_index}] Navegando a Indeed: {page_url}")
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"[!] Error al navegar en Indeed: {e}")

            extracted = await parse_and_extract_jobs(page)
            if not extracted:
                print(f"[!] No se detectaron mas tarjetas en offset {start_index}. Finalizando extraccion.")
                break

            print(f"[+] Se encontraron {len(extracted)} puestos de trabajo en la pagina.")
            for item in extracted:
                all_jobs.append(item)
                if len(all_jobs) >= max_leads:
                    break

            start_index += 10
            await asyncio.sleep(random.uniform(2.0, 3.5))

        await context.close()

    print(f"\n[OK] Total de empleos identificados: {len(all_jobs)}")

    # Fase 2: Crawler de emails para empresas con website identificado
    if crawl_emails:
        print("\n" + "-" * 65)
        print(" [*] PROCESANDO CRAWLER DE EMAILS PARA EMPRESAS...")
        print("-" * 65)
        for idx, job in enumerate(all_jobs, 1):
            web = job.get("Website", "")
            if web and web != "N/A" and web.startswith("http") and "indeed.com" not in web:
                print(f"[{idx}/{len(all_jobs)}] Explorando web: {web}...")
                email = extract_emails_from_site(web, timeout=6)
                if email:
                    print(f"    --> [EMAIL ENCONTRADO]: {email}")
                    job["Email"] = email
            else:
                # En Indeed la tarjeta no siempre da la web externa directa
                job["Email"] = ""

    # Fase 3: Exportar a Excel con Pandas
    df = pd.DataFrame(all_jobs)
    cols = ["Business_Name", "City", "Job_Title", "Job_URL", "Website", "Email"]
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols]

    # Guardar en Excel
    df.to_excel(output_path, index=False, engine="openpyxl")
    print("\n" + "=" * 65)
    print(f"[EXITO] Archivo generado: {output_path}")
    print(f"Total registros exportados: {len(df)}")
    print("=" * 65)
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scraper de Empleos Indeed con Playwright y BeautifulSoup")
    parser.add_argument("--url", default=DEFAULT_INDEED_URL, help="URL de busqueda en Indeed.com")
    parser.add_argument("--target", type=int, default=15, help="Numero maximo de leads a extraer")
    parser.add_argument("--output", default="indeed_hiring_leads.xlsx", help="Ruta del archivo Excel de salida")
    parser.add_argument("--no-emails", action="store_true", help="Desactivar crawler de emails")

    args = parser.parse_args()
    asyncio.run(run_indeed_scraper(
        target_url=args.url,
        max_leads=args.target,
        crawl_emails=not args.no_emails,
        output_path=args.output
    ))
