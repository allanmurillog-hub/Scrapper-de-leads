"""
Modulo Universal de Extraccion y Limpieza de Emails para Lead Generation B2B
Autor: Senior Python Data Engineer
"""
import re
import urllib.parse
from typing import Optional, Set
import requests

# Expresion regular rigurosa para correos B2B
EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,10}",
    re.IGNORECASE
)

# Extensiones falsas y dominios de ruido a descartar
INVALID_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", 
    ".ico", ".bmp", ".tiff", ".css", ".js", ".woff", ".woff2"
)

BLOCKED_EMAIL_KEYWORDS = {
    "sentry", "wixpress", "example.com", "domain.com", 
    "email.com", "yourdomain", "test@", "schema.org",
    "git@", "noreply", "no-reply", "donotreply", "privacy@",
    "abuse@", "mailer-daemon", "user@", "wght@", "font@", "rating@"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
}

def is_valid_email(email: str) -> bool:
    """Valida que un correo sea un lead humano o corporativo real."""
    if not email:
        return False
    email_lower = email.lower().strip()
    
    # Descartar si contiene doble punto consecutivo (artefacto de CSS como ..700)
    if ".." in email_lower:
        return False

    # Descartar si termina en una extension de imagen o recurso
    if any(email_lower.endswith(ext) for ext in INVALID_EXTENSIONS):
        return False
        
    # Descartar si contiene palabras de telemetria, bots o templates
    if any(kw in email_lower for kw in BLOCKED_EMAIL_KEYWORDS):
        return False

    # Verificar que el dominio termine en letras legitimas (TLD)
    parts = email_lower.split("@")
    if len(parts) != 2:
        return False
    user_part, domain_part = parts
    if not user_part or not domain_part or "." not in domain_part:
        return False
        
    tld = domain_part.split(".")[-1]
    if not tld.isalpha() or len(tld) < 2:
        return False

    # Verificar longitud sensata
    if len(email_lower) < 6 or len(email_lower) > 70:
        return False
        
    return True

def clean_company_name(name: str) -> str:
    """
    Remueve sufijos corporativos legales y signos de puntuacion finales
    para personalizacion natural en cold email:
    'Smith Plumbing, LLC.' -> 'Smith Plumbing'
    """
    if not name or name == "N/A":
        return ""
    # Remover sufijos comunes
    legal_suffixes = r"\b(LLC|INC|CORP|CORPORATION|CO|LTD|LP|LLP|PLLC|GMBH|S\.A\.|S\.L\.)\b\.?"
    cleaned = re.sub(legal_suffixes, "", name, flags=re.IGNORECASE)
    # Limpiar comas, puntos y espacios extras al final o principio
    cleaned = re.sub(r"[\s,\.\-]+$", "", cleaned).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned if cleaned else name.strip()

def clean_city_string(raw_location: str) -> str:
    """
    Aisla unicamente la Ciudad para usarla directamente como variable {{City}} en cold email.
    Ejemplos:
      'San Diego, CA' -> 'San Diego'
      'Hybrid work in Jacksonville, FL' -> 'Jacksonville'
      'Orlando, FL 32827' -> 'Orlando'
      'Miami, FL' -> 'Miami'
    """
    if not raw_location or raw_location == "N/A":
        return ""
    text = raw_location.strip()
    # Si contiene ' in ', extraer lo que sigue (ej. 'Hybrid work in Jacksonville, FL')
    if " in " in text:
        text = text.split(" in ")[-1].strip()
    # Quitar codigos postales (5 digitos al final)
    text = re.sub(r"\b\d{5}(-\d{4})?\b", "", text).strip()
    # Si viene con estado 'Ciudad, Estado', separar por coma
    if "," in text:
        city_candidate = text.split(",")[0].strip()
        # Eliminar prefijos comunes como 'Remote', 'Hybrid'
        city_candidate = re.sub(r"^(Remote|Hybrid|Work from home)\s*", "", city_candidate, flags=re.IGNORECASE).strip()
        if city_candidate:
            return city_candidate
    return text

def extract_real_website_url(url: str) -> str:
    """
    Desempaqueta redirects de plataformas como Clutch.co:
    'https://r.clutch.co/redirect?...&u=https%3A%2F%2Fagency.com%2F...' -> 'https://agency.com'
    """
    if not url or url == "N/A":
        return ""
    if "clutch.co/redirect" in url:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        if "u" in qs and qs["u"]:
            target = urllib.parse.unquote(qs["u"][0])
            # Quitar parametros UTM de tracking
            return target.split("?")[0].rstrip("/")
    return url.split("?")[0].rstrip("/")

def extract_emails_from_site(website_url: str, timeout: int = 7) -> str:
    """
    Visita el sitio web del cliente y busca emails con Regex.
    Si no encuentra en la home, intenta en paginas de contacto clave (/contact, /about).
    Retorna el primer email valido encontrado o string vacio.
    """
    real_url = extract_real_website_url(website_url)
    if not real_url or not real_url.startswith("http"):
        return ""

    found_emails: Set[str] = set()
    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. Probar en la pagina principal
    try:
        resp = session.get(real_url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            for match in EMAIL_REGEX.findall(resp.text):
                if is_valid_email(match):
                    found_emails.add(match.lower())
    except Exception:
        pass

    if found_emails:
        # Priorizar correos corporativos como contact@, info@, hello@
        sorted_emails = sorted(
            list(found_emails),
            key=lambda e: (
                0 if any(p in e for p in ["contact", "info", "hello", "sales", "office"]) else 1
            )
        )
        return sorted_emails[0]

    # 2. Si no hubo exito en la Home, probar en subpaginas comunes
    contact_endpoints = ["/contact", "/contact-us", "/about", "/about-us"]
    base_domain = f"{urllib.parse.urlparse(real_url).scheme}://{urllib.parse.urlparse(real_url).netloc}"
    
    for endpoint in contact_endpoints:
        sub_url = f"{base_domain}{endpoint}"
        try:
            sub_resp = session.get(sub_url, timeout=timeout, allow_redirects=True)
            if sub_resp.status_code == 200:
                for match in EMAIL_REGEX.findall(sub_resp.text):
                    if is_valid_email(match):
                        found_emails.add(match.lower())
                if found_emails:
                    break
        except Exception:
            continue

    if found_emails:
        sorted_emails = sorted(
            list(found_emails),
            key=lambda e: (
                0 if any(p in e for p in ["contact", "info", "hello", "sales", "office"]) else 1
            )
        )
        return sorted_emails[0]

    return ""
