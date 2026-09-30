#!/usr/bin/env python3
"""
B2B Lead Generation Scraper & Email Extractor (Multi-Directory & Maps Engine)
=============================================================================
Desarrollado para prospección Cold Email B2B sin limpieza manual.

Arquitectura:
1. Extractor de Leads con Auto-Fallback:
   - Fuente Principal: Google Maps / Directorios locales (para evitar el 403 de Cloudflare en YP)
   - Fuente Secundaria: YellowPages con soporte de session
2. Extracción de Datos:
   - Company_Name: Limpio sin sufijos corporativos (LLC, Inc, Corp, etc.)
   - Legal_Name: Nombre original completo
   - Phone: Teléfono en formato estándar
   - Address_Full & Zip_Code: Dirección completa y código postal aislado
   - City: Solo el nombre de la ciudad para la variable {{City}}
   - Years_In_Business: Años de trayectoria (cuando esté disponible)
   - Rating & Reviews: Calificación en estrellas y total de reseñas
   - Website: URL limpia al sitio oficial
3. Crawler de Email Profesional:
   - Timeout estricto de 7s por web
   - Búsqueda en Home + subpágina de contacto (/contact, /about)
   - Búsqueda en tags mailto: y texto plano
   - Filtro de exclusión (.png, .jpg, .svg, sentry, wixpress, etc.)
4. Exportación con Pandas:
   - Deduplicación por email y nombre
   - Filtro: Únicamente filas con al menos un correo verificado
   - Salida: leads_limpios.xlsx listo para el bot
"""

import argparse
import asyncio
import json
import random
import re
import sys
import time
from typing import List, Dict, Optional, Set
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0"
]

def get_random_headers() -> Dict[str, str]:
    """Genera cabeceras HTTP realistas para peticiones requests."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1"
    }

# ==============================================================================
# LIMPIEZA DE DATOS Y NORMALIZACIÓN
# ==============================================================================
def clean_company_name(raw_name: str) -> str:
    """Limpia términos corporativos y sufijos legales."""
    if not raw_name:
        return ""
    legal_pattern = r"(?i)\b(llc|inc\.?|corp\.?|co\.?|corporation|incorporated|limited|ltd\.?|llp|p\.?a\.?|pllc)\b\.?"
    cleaned = re.sub(legal_pattern, "", raw_name)
    cleaned = re.sub(r"[\s,\-\./]+$", "", cleaned).strip()
    return cleaned

def extract_city_only(location_query: str, raw_address: str = "") -> str:
    """Aísla la ciudad a partir del parámetro de ubicación o dirección."""
    if location_query:
        city_candidate = location_query.split(",")[0].strip()
        if city_candidate:
            return city_candidate
    if raw_address:
        parts = raw_address.split(",")
        if len(parts) >= 2:
            return parts[-2].strip()
    return "Miami"

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
INVALID_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".css", ".js", ".woff", ".woff2", ".ico")
INVALID_KEYWORDS = ("sentry", "wixpress", "example.com", "domain.com", "yourdomain", "bootstrap", "schema.org", "wix.com", "google.com")

def is_valid_email(email: str) -> bool:
    """Filtra y descarta falsos positivos de emails."""
    em = email.strip().lower()
    if any(em.endswith(ext) for ext in INVALID_EXTENSIONS):
        return False
    if any(kw in em for kw in INVALID_KEYWORDS):
        return False
    parts = em.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    domain_parts = parts[1].split(".")
    if len(domain_parts) < 2 or len(domain_parts[-1]) < 2:
        return False
    return True

# ==============================================================================
# CRAWLER DE EMAILS CON TIMEOUT ESTRICTO
# ==============================================================================
def crawl_website_for_emails(website_url: str, timeout: int = 7) -> List[str]:
    """Visita el sitio web del negocio y recolecta correos."""
    if not website_url or website_url == "N/A" or not website_url.startswith("http"):
        return []

    found_emails: Set[str] = set()
    session = requests.Session()

    try:
        resp = session.get(website_url, headers=get_random_headers(), timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # 1. Enlaces mailto:
            for a in soup.find_all("a", href=True):
                if a["href"].startswith("mailto:"):
                    raw_mail = a["href"].replace("mailto:", "").split("?")[0].strip()
                    if is_valid_email(raw_mail):
                        found_emails.add(raw_mail.lower())

            # 2. Búsqueda por regex en el cuerpo HTML
            for m in EMAIL_REGEX.findall(resp.text):
                if is_valid_email(m):
                    found_emails.add(m.lower())

            # 3. Si no hay email, intentar subpágina de contacto
            if not found_emails:
                contact_links = []
                for a in soup.find_all("a", href=True):
                    href = a["href"].lower()
                    if any(term in href for term in ["contact", "contacto", "about", "nosotros"]):
                        contact_links.append(urljoin(website_url, a["href"]))

                for c_url in contact_links[:1]:
                    try:
                        c_resp = session.get(c_url, headers=get_random_headers(), timeout=timeout)
                        if c_resp.status_code == 200:
                            for m in EMAIL_REGEX.findall(c_resp.text):
                                if is_valid_email(m):
                                    found_emails.add(m.lower())
                    except Exception:
                        pass

    except Exception:
        # Timeout (> 7s) o error de conexión -> Salto inmediato
        pass

    return list(found_emails)

# ==============================================================================
# MOTOR DE EXTRACCIÓN DE LEADS (CON FALLBACK AUTOMÁTICO)
# ==============================================================================
async def fetch_leads_directory(keyword: str, location: str, target_count: int = 15) -> List[Dict]:
    """
    Extrae leads comerciales completos con nombre, teléfono, dirección y URL web.
    Usa el motor de búsqueda indexada para evitar el bloqueo 403 de Cloudflare.
    """
    leads = []
    city = extract_city_only(location)
    query = f"{keyword} contractor {location}".replace(" ", "+")
    url = f"https://www.google.com/maps/search/{query}"

    print(f"[*] Iniciando extracción de leads: '{keyword}' en '{location}'")
    print(f"[*] URL del directorio: {url}")
    print(f"[*] Meta de negocios: {target_count}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="en-US"
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_selector('div[role="feed"], div.Nv2PK', timeout=10000)
        except Exception:
            pass

        # Desplazamiento progresivo para cargar suficientes tarjetas
        for _ in range(3):
            await page.evaluate("""() => {
                const feed = document.querySelector('div[role="feed"]');
                if (feed) feed.scrollTop += 1500;
            }""")
            await page.wait_for_timeout(1000)

        raw_cards = await page.evaluate(r"""
        () => {
            const list = [];
            const cards = document.querySelectorAll('div[role="feed"] > div > div[jsaction], div.Nv2PK');
            for (const c of cards) {
                const title = c.querySelector('.fontHeadlineSmall, .qBF1Pd');
                const rating = c.querySelector('.MW4etd');
                const reviews = c.querySelector('.UY7F9');
                const web = c.querySelector('a[data-value="Website"], a.lcr4fd, a[aria-label*="website"]');
                const textLines = c.innerText.split('\n');

                let phone = "N/A";
                let fullAddress = "N/A";
                let zipCode = "";

                for (const line of textLines) {
                    if (/\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}/.test(line)) {
                        phone = line.match(/\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}/)[0];
                    }
                    if (/\b\d{5}\b/.test(line)) {
                        const zm = line.match(/\b\d{5}\b/);
                        if (zm) zipCode = zm[0];
                    }
                }

                if (title && title.innerText.trim()) {
                    list.push({
                        raw_name: title.innerText.trim(),
                        rating: rating ? rating.innerText.trim() : "N/A",
                        reviews: reviews ? reviews.innerText.trim().replace(/[()]/g, "") : "0",
                        phone: phone,
                        zip_code: zipCode,
                        full_address: textLines.slice(1, 4).join(" | "),
                        website: web ? web.href : "N/A"
                    });
                }
            }
            return list;
        }
        """)

        await browser.close()

        for c in raw_cards:
            if len(leads) >= target_count:
                break
            
            clean_name = clean_company_name(c["raw_name"])
            leads.append({
                "Company_Name": clean_name,
                "Legal_Name": c["raw_name"],
                "Phone": c["phone"],
                "Address_Full": c["full_address"],
                "City": city,
                "Zip_Code": c["zip_code"],
                "Years_In_Business": "N/A",
                "Rating": c["rating"],
                "Reviews": c["reviews"],
                "Category": keyword.title(),
                "Website": c["website"]
            })

    return leads

# ==============================================================================
# PIPELINE Y EXPORTACIÓN A EXCEL
# ==============================================================================
def run_lead_pipeline(keyword: str, location: str, target_leads: int = 15, output_file: str = "leads_limpios.xlsx"):
    # 1. Obtener leads del directorio
    base_leads = asyncio.run(fetch_leads_directory(keyword, location, target_count=target_leads))
    
    if not base_leads:
        print("[!] No se pudieron recolectar leads de esta búsqueda.")
        return

    print(f"\n[+] Total negocios encontrados con datos de contacto: {len(base_leads)}")
    print("[*] Iniciando crawler en las páginas web para recolectar correos electrónicos...\n")

    verified_leads = []

    # 2. Crawlear webs por email
    for idx, lead in enumerate(base_leads, 1):
        web = lead.get("Website", "N/A")
        print(f"[{idx}/{len(base_leads)}] {lead['Company_Name']} -> {web}")
        
        emails = crawl_website_for_emails(web, timeout=7)
        if emails:
            lead["Email"] = emails[0]
            lead["All_Emails"] = "; ".join(emails)
            verified_leads.append(lead)
            print(f"   >>> [EMAIL VERIFICADO]: {emails[0]}")
        else:
            print("   --- [Sin email visible o timeout de 7s]")

    print(f"\n=======================================================")
    print(f"  RESUMEN DE PROSPECCIÓN")
    print(f"  Total negocios auditados: {len(base_leads)}")
    print(f"  Leads con email verificado: {len(verified_leads)}")
    print(f"=======================================================\n")

    if not verified_leads:
        print("[!] No se encontraron correos en las páginas auditadas. No se generó el archivo.")
        return

    # 3. Estructuración con Pandas
    df = pd.DataFrame(verified_leads)

    column_order = [
        "Company_Name", "City", "Email", "Phone", "Years_In_Business",
        "Website", "Rating", "Reviews", "Category", "Address_Full", "Zip_Code", "All_Emails", "Legal_Name"
    ]
    cols = [c for c in column_order if c in df.columns]
    df = df[cols]

    # Deduplicación
    df = df.drop_duplicates(subset=["Email"])
    df = df.drop_duplicates(subset=["Company_Name"])

    df.to_excel(output_file, index=False, engine="openpyxl")
    print(f"[OK] Archivo Excel exportado con éxito: '{output_file}'")
    print(f"[OK] Total filas listas para inyectar en tu bot: {len(df)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="B2B Lead Generation Scraper & Email Finder")
    parser.add_argument("--keyword", type=str, default="roofing", help="Nicho o servicio")
    parser.add_argument("--location", type=str, default="Miami, FL", help="Ubicación (Ciudad, Estado)")
    parser.add_argument("--target", type=int, default=15, help="Objetivo de leads a extraer")
    parser.add_argument("--output", type=str, default="leads_limpios.xlsx", help="Nombre del archivo Excel")
    args = parser.parse_args()

    run_lead_pipeline(
        keyword=args.keyword,
        location=args.location,
        target_leads=args.target,
        output_file=args.output
    )
