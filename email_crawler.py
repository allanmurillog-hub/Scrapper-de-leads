"""
=============================================================================
Modulo Universal de Extraccion, Limpieza y Validacion de Leads B2B
Autor: Senior Python Data Engineer
=============================================================================
Motor central compartido por todos los scrapers del sistema.
Incluye: validacion de emails, limpieza de nombres corporativos,
aislamiento de ciudades, desofuscacion de URLs, validacion HTTP,
filtro anti-fake-data y crawler de emails multi-pagina.
"""
import re
import os
import random
import urllib.parse
from typing import Optional, Set, List
import requests

# ============================================================================
# REGEX Y LISTAS DE BLOQUEO
# ============================================================================

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,10}",
    re.IGNORECASE
)

INVALID_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif",
    ".ico", ".bmp", ".tiff", ".css", ".js", ".woff", ".woff2"
)

BLOCKED_EMAIL_KEYWORDS = {
    "sentry", "wixpress", "example.com", "domain.com",
    "email.com", "yourdomain", "test@", "schema.org",
    "git@", "noreply", "no-reply", "donotreply", "privacy@",
    "abuse@", "mailer-daemon", "user@", "wght@", "font@", "rating@",
    "dummy", "email@", "info@example.com", "test.com", "yoursite.com",
    "sentry.io",
}

# Nombres de empresa que son placeholders o ruido de la web
FAKE_COMPANY_NAMES = {
    "confidential", "apply now", "indeed", "featured", "home",
    "n/a", "test", "company", "your company", "hiring",
    "unknown", "various", "see description",
}

# Lista de User-Agents reales para rotacion
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]

def _get_headers() -> dict:
    """Retorna headers HTTP con User-Agent rotado aleatoriamente."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
    }

# ============================================================================
# VALIDACION DE EMAILS
# ============================================================================

def is_valid_email(email: str) -> bool:
    """Valida que un correo sea un lead humano o corporativo real."""
    if not email:
        return False
    email_lower = email.lower().strip()

    # Artefactos de CSS como wght@200..700
    if ".." in email_lower:
        return False

    # Extensiones de imagen o recurso
    if any(email_lower.endswith(ext) for ext in INVALID_EXTENSIONS):
        return False

    # Palabras de telemetria, bots o templates
    if any(kw in email_lower for kw in BLOCKED_EMAIL_KEYWORDS):
        return False

    # Estructura basica del dominio
    parts = email_lower.split("@")
    if len(parts) != 2:
        return False
    user_part, domain_part = parts
    if not user_part or not domain_part or "." not in domain_part:
        return False

    tld = domain_part.split(".")[-1]
    if not tld.isalpha() or len(tld) < 2:
        return False

    # Longitud sensata
    if len(email_lower) < 6 or len(email_lower) > 70:
        return False

    return True

# ============================================================================
# LIMPIEZA DE DATOS
# ============================================================================

def clean_company_name(name: str) -> str:
    """
    Remueve sufijos corporativos legales y signos de puntuacion finales.
    'Smith Plumbing, LLC.' -> 'Smith Plumbing'
    """
    if not name or name == "N/A":
        return ""
    legal_suffixes = r"\b(LLC|INC|CORP|CORPORATION|CO|LTD|LP|LLP|PLLC|GMBH|S\.A\.|S\.L\.)\b\.?"
    cleaned = re.sub(legal_suffixes, "", name, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\s,\.\-]+$", "", cleaned).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned if cleaned else name.strip()


def clean_city_string(raw_location: str) -> str:
    """
    Aisla unicamente la Ciudad para usarla como variable {{City}}.
    'Hybrid work in Jacksonville, FL' -> 'Jacksonville'
    """
    if not raw_location or raw_location == "N/A":
        return ""
    text = raw_location.strip()
    if " in " in text:
        text = text.split(" in ")[-1].strip()
    text = re.sub(r"\b\d{5}(-\d{4})?\b", "", text).strip()
    if "," in text:
        city_candidate = text.split(",")[0].strip()
        city_candidate = re.sub(
            r"^(Remote|Hybrid|Work from home)\s*", "",
            city_candidate, flags=re.IGNORECASE
        ).strip()
        if city_candidate:
            return city_candidate
    return text

# ============================================================================
# VALIDACION DE CALIDAD DE LEADS (ANTI-FAKE DATA)
# ============================================================================

def validate_lead_quality(lead: dict) -> bool:
    """
    Valida que un lead sea real y no un placeholder o dato basura.
    Retorna True si pasa todos los filtros de calidad.
    """
    # 1. Nombre de empresa valido
    name = lead.get("Business_Name", "").strip()
    if not name or len(name) < 3:
        return False
    if name.lower() in FAKE_COMPANY_NAMES:
        return False

    # 2. Website no sea un dominio dummy (si tiene website)
    website = lead.get("Website", "").strip()
    if website and website != "N/A":
        domain_lower = website.lower()
        dummy_domains = [
            "example.com", "domain.com", "yoursite.com",
            "test.com", "sample.com", "wixpress.com", "sentry.io"
        ]
        if any(dd in domain_lower for dd in dummy_domains):
            return False

    # 3. Email no sea placeholder (si tiene email)
    email = lead.get("Email", "").strip()
    if email and email != "N/A":
        if not is_valid_email(email):
            return False

    return True

# ============================================================================
# VALIDACION HTTP DE SITIOS WEB
# ============================================================================

def is_website_live(url: str, timeout: int = 5) -> bool:
    """
    Verifica rapidamente si un sitio web responde con status valido.
    Intenta HEAD primero (mas rapido), con fallback a GET si HEAD da 405.
    """
    if not url or not url.startswith("http"):
        return False
    try:
        resp = requests.head(
            url, allow_redirects=True, timeout=timeout,
            headers=_get_headers()
        )
        if resp.status_code == 405:
            # Servidor no acepta HEAD, intentar GET con stream
            resp = requests.get(
                url, allow_redirects=True, timeout=timeout,
                headers=_get_headers(), stream=True
            )
        return resp.status_code < 400
    except (requests.RequestException, Exception):
        return False

# ============================================================================
# DESOFUSCACION DE URLS
# ============================================================================

def extract_real_website_url(url: str) -> str:
    """
    Desempaqueta redirects de plataformas como Clutch.co y limpia UTMs.
    """
    if not url or url == "N/A":
        return ""

    target_url = url
    if "clutch.co/redirect" in url:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if "u" in qs and qs["u"]:
            target_url = urllib.parse.unquote(qs["u"][0])

    # Resolver redirecciones HTTP reales
    try:
        resp = requests.head(
            target_url, allow_redirects=True, timeout=5,
            headers=_get_headers()
        )
        if resp.url:
            target_url = resp.url
    except (requests.RequestException, Exception):
        pass

    # Limpiar parametros UTM y de tracking
    parsed = urllib.parse.urlparse(target_url)
    clean_params = {
        k: v for k, v in urllib.parse.parse_qs(parsed.query).items()
        if not k.lower().startswith(("utm_", "ref", "source", "campaign"))
    }
    clean_query = urllib.parse.urlencode(clean_params, doseq=True)
    clean_url = urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, clean_query, "")
    )
    return clean_url.rstrip("/")

# ============================================================================
# CRAWLER DE EMAILS MULTI-PAGINA
# ============================================================================

def extract_emails_from_site(website_url: str, timeout: int = 6) -> str:
    """
    Visita el sitio web del cliente y busca emails con Regex.
    Si no encuentra en la home, intenta en paginas de contacto clave.
    Valida la conectividad HTTP antes de iniciar el rastreo.
    """
    real_url = extract_real_website_url(website_url)
    if not real_url or not real_url.startswith("http"):
        return ""

    # Pre-flight check: verificar que el sitio este vivo
    if not is_website_live(real_url, timeout=5):
        return ""

    found_emails: Set[str] = set()
    session = requests.Session()
    session.headers.update(_get_headers())

    # 1. Rastrear la pagina principal
    try:
        resp = session.get(real_url, timeout=timeout, allow_redirects=True)
        if resp.status_code < 400:
            for match in EMAIL_REGEX.findall(resp.text):
                if is_valid_email(match):
                    found_emails.add(match.lower())
    except (requests.RequestException, Exception):
        pass

    # 2. Si no hubo emails en la Home, probar subpaginas canónicas
    if not found_emails:
        contact_endpoints = ["/contact", "/contact-us", "/about", "/about-us"]
        base = f"{urllib.parse.urlparse(real_url).scheme}://{urllib.parse.urlparse(real_url).netloc}"

        for endpoint in contact_endpoints:
            sub_url = f"{base}{endpoint}"
            try:
                sub_resp = session.get(sub_url, timeout=timeout, allow_redirects=True)
                if sub_resp.status_code < 400:
                    for match in EMAIL_REGEX.findall(sub_resp.text):
                        if is_valid_email(match):
                            found_emails.add(match.lower())
                    if found_emails:
                        break  # Encontramos al menos uno, no seguir
            except (requests.RequestException, Exception):
                continue

    if found_emails:
        # Priorizar correos corporativos genericos
        sorted_emails = sorted(
            list(found_emails),
            key=lambda e: (
                0 if any(p in e for p in ["contact", "info", "hello", "sales", "office"]) else 1
            )
        )
        return sorted_emails[0]

    return ""


# ============================================================================
# DETECCION DE BLOQUEOS ANTI-BOT (Para uso en scrapers Playwright)
# ============================================================================

CLOUDFLARE_INDICATORS = [
    "just a moment", "cloudflare", "security check",
    "attention required", "verify you are human",
    "checking your browser", "ray id",
]

def detect_antibot_block(page_title: str, html_snippet: str = "") -> bool:
    """
    Detecta si Cloudflare o un WAF esta bloqueando la pagina.
    Retorna True si se detecta bloqueo.
    """
    combined = (page_title + " " + html_snippet[:2000]).lower()
    return any(indicator in combined for indicator in CLOUDFLARE_INDICATORS)


# ============================================================================
# DUMP DE DIAGNOSTICO
# ============================================================================

def save_debug_dump(html: str, filename: str = "debug_page_dump.html"):
    """
    Guarda el HTML de una pagina que devolvio 0 tarjetas para diagnostico.
    """
    dump_dir = os.path.dirname(os.path.abspath(__file__))
    dump_path = os.path.join(dump_dir, filename)
    try:
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[DEBUG] HTML de diagnostico guardado en: {dump_path}")
    except Exception as e:
        print(f"[DEBUG] No se pudo guardar dump: {e}")
