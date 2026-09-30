"""
=============================================================================
SCRAPER 1: Agencias Digitales de Alto Valor (Clutch.co)
Tecnologia: Playwright Asincrono + BeautifulSoup (Inspector Universal) + Pandas
=============================================================================
Extrae perfiles de agencias de Clutch.co con evasión de Cloudflare, extrae el HTML
completo con `page.content()` y lo procesa con BeautifulSoup.
Visita los sitios web de las agencias para extraer emails limpios para Cold Outreach B2B.
"""

import argparse
import asyncio
import os
import random
import sys
from typing import Dict, List
from bs4 import BeautifulSoup
import pandas as pd
from playwright.async_api import async_playwright

# Importar funciones de limpieza y crawler de emails
try:
    from email_crawler import (
        clean_company_name,
        clean_city_string,
        extract_real_website_url,
        extract_emails_from_site,
    )
except ImportError:
    # Soporte por si se ejecuta desde otro directorio
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from email_crawler import (
        clean_company_name,
        clean_city_string,
        extract_real_website_url,
        extract_emails_from_site,
    )

DEFAULT_CLUTCH_URL = "https://clutch.co/agencies/digital-marketing"
BROWSER_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clutch_browser_data")


async def fetch_page_html(url: str, user_data_dir: str) -> str:
    """
    Carga la pagina utilizando Playwright con contexto persistente y Chrome real
    para superar Cloudflare de forma limpia y transparente, y extrae el HTML universal.
    """
    async with async_playwright() as p:
        # Contexto persistente para mantener cookies y huella valida
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
        
        # Retardo aleatorio para comportamiento humano
        await asyncio.sleep(random.uniform(0.8, 1.5))
        
        print(f"[*] Navegando a: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[!] Error o timeout al cargar URL: {e}")
            
        # Esperar a que se rendericen los componentes dinamicos
        wait_time = random.uniform(2.5, 4.0)
        await asyncio.sleep(wait_time)
        
        # Extraer el HTML completo de la pagina (Patron Inspector Universal)
        html_content = await page.content()
        await context.close()
        return html_content


def parse_clutch_agencies(html: str) -> List[Dict[str, str]]:
    """
    Parsea el HTML universal de Clutch con BeautifulSoup.
    Extrae Business_Name, City, Min_Project_Size y Website.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Tarjetas de proveedores en Clutch
    cards = soup.select(".provider-row, li.provider-row, .directory-list li.provider")
    
    agencies: List[Dict[str, str]] = []
    seen_names = set()

    for card in cards:
        # 1. Nombre de la empresa
        name_el = card.select_one("a.company_title, .company_info a, .provider__title a, h3 a")
        raw_name = name_el.get_text(strip=True) if name_el else ""
        clean_name = clean_company_name(raw_name)
        
        if not clean_name or clean_name.lower() in seen_names:
            continue
            
        # 2. Ciudad / Ubicacion
        loc_el = card.select_one(".locality, .location, .provider-detail__item--location, .provider__highlights-item--location")
        raw_loc = loc_el.get_text(strip=True) if loc_el else ""
        city = clean_city_string(raw_loc)
        
        # 3. Presupuesto minimo (Min_Project_Size)
        min_proj_el = card.select_one(
            ".min-project-size, [class*='min-project-size'], "
            ".provider__highlights-item.sg-tooltip-v2"
        )
        min_project_size = "N/A"
        if min_proj_el:
            min_project_size = min_proj_el.get_text(strip=True)
        else:
            # Fallback buscando en items de caracteristicas
            highlights = card.select(".provider__highlights-item")
            for h in highlights:
                txt = h.get_text(strip=True)
                if "$" in txt and ("+" in txt or "<" in txt or "," in txt):
                    min_project_size = txt
                    break
                    
        # 4. Enlace al sitio web oficial
        web_el = card.select_one(
            "a.website-link__item, a.visit-website, a[data-link_text='website'], "
            "a[href*='clutch.co/redirect'], a[href*='visit_website']"
        )
        raw_web = web_el.get("href") if web_el else ""
        clean_web = extract_real_website_url(raw_web)
        
        seen_names.add(clean_name.lower())
        agencies.append({
            "Business_Name": clean_name,
            "City": city if city else "N/A",
            "Min_Project_Size": min_project_size,
            "Website": clean_web if clean_web else "N/A",
            "Email": ""  # Se completará con el crawler
        })

    return agencies


async def parse_and_extract_cards(page, current_page: int) -> List[Dict[str, str]]:
    """
    Espera a que aparezcan las tarjetas en el DOM y extrae el HTML universal.
    """
    try:
        await page.wait_for_selector(".provider-row, li.provider-row, .directory-list li", timeout=15000)
    except Exception:
        print("[!] Timeout esperando selector de tarjetas. Procediendo con el HTML disponible...")

    await asyncio.sleep(random.uniform(1.5, 2.5))
    html = await page.content()
    return parse_clutch_agencies(html)


async def run_clutch_scraper(
    target_url: str = DEFAULT_CLUTCH_URL,
    max_leads: int = 15,
    crawl_emails: bool = True,
    output_path: str = "clutch_agencies_leads.xlsx"
):
    """
    Orquesta la extraccion de agencias en Clutch.co y la resolucion de emails
    manteniendo la sesion de navegador activa.
    """
    print("=" * 65)
    print(" [*] INICIANDO SCRAPER 1: CLUTCH.CO (Agencias Digitales)")
    print(f" [*] URL Objetivo: {target_url}")
    print(f" [*] Leads solicitados: {max_leads}")
    print("=" * 65)

    all_agencies: List[Dict[str, str]] = []
    current_page = 0

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
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

        while len(all_agencies) < max_leads:
            # Paginacion en Clutch: ?page=0, ?page=1, ?page=2...
            separator = "&" if "?" in target_url else "?"
            page_url = f"{target_url}{separator}page={current_page}" if current_page > 0 else target_url
            
            print(f"\n--> [Página {current_page + 1}] Navegando a: {page_url}")
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"[!] Error al navegar: {e}")

            extracted = await parse_and_extract_cards(page, current_page)
            if not extracted:
                print(f"[!] No se encontraron mas agencias en la pagina {current_page + 1}. Finalizando paginacion.")
                break
                
            print(f"[+] Se identificaron {len(extracted)} agencias en la pagina.")
            for item in extracted:
                all_agencies.append(item)
                if len(all_agencies) >= max_leads:
                    break
                    
            current_page += 1
            await asyncio.sleep(random.uniform(2.0, 3.5))

        await context.close()

    print(f"\n[OK] Total de agencias recolectadas: {len(all_agencies)}")

    # Fase 2: Crawler de Emails sobre los Sitios Web descubiertos
    if crawl_emails:
        print("\n" + "-" * 65)
        print(" [*] INICIANDO CRAWLER DE EMAILS B2B...")
        print("-" * 65)
        
        for idx, agency in enumerate(all_agencies, 1):
            web = agency.get("Website", "")
            if web and web != "N/A" and web.startswith("http"):
                print(f"[{idx}/{len(all_agencies)}] Explorando web: {web}...")
                email = extract_emails_from_site(web, timeout=6)
                if email:
                    print(f"    --> [EMAIL ENCONTRADO]: {email}")
                    agency["Email"] = email
                else:
                    print("    --> No se encontro email publico o sin captcha.")
            else:
                print(f"[{idx}/{len(all_agencies)}] Sin URL valida de website.")

    # Fase 3: Exportar a Excel con Pandas
    df = pd.DataFrame(all_agencies)
    
    # Reordenar columnas estrictas
    cols = ["Business_Name", "City", "Min_Project_Size", "Website", "Email"]
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
    parser = argparse.ArgumentParser(description="Scraper de Agencias Clutch.co con Playwright y BeautifulSoup")
    parser.add_argument("--url", default=DEFAULT_CLUTCH_URL, help="URL de categoria en Clutch.co")
    parser.add_argument("--target", type=int, default=15, help="Numero maximo de leads a extraer")
    parser.add_argument("--output", default="clutch_agencies_leads.xlsx", help="Ruta del archivo Excel de salida")
    parser.add_argument("--no-emails", action="store_true", help="Desactivar crawler de emails")

    args = parser.parse_args()
    asyncio.run(run_clutch_scraper(
        target_url=args.url,
        max_leads=args.target,
        crawl_emails=not args.no_emails,
        output_path=args.output
    ))
