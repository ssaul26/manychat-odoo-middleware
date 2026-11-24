from fastapi import APIRouter, Header, BackgroundTasks
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import html
import json
import logging
import os
import re
import unicodedata
import uuid
import time
import tempfile
import threading
import xmlrpc.client
from difflib import SequenceMatcher
from urllib.parse import urljoin

import openai
import requests
from openai import OpenAI
from pydantic import field_validator

router = APIRouter()
logger = logging.getLogger("sporthouse-ai")

ODOO_URL = (os.getenv("ODOO_URL") or "").rstrip("/") or None
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_KNOWLEDGE_VECTOR_STORE_ID = os.getenv("OPENAI_KNOWLEDGE_VECTOR_STORE_ID")
OPENAI_KNOWLEDGE_VECTOR_STORE_NAME = os.getenv("OPENAI_KNOWLEDGE_VECTOR_STORE_NAME", "SportHouse Knowledge")
KNOWLEDGE_SYNC_KEY = os.getenv("KNOWLEDGE_SYNC_KEY")
# Umbral de relevancia para la herramienta buscar_info_escuela (antes 0.12: dejaba pasar fragmentos irrelevantes).
KNOWLEDGE_SCORE_THRESHOLD = float(os.getenv("KNOWLEDGE_SCORE_THRESHOLD", "0.3"))
# La precarga automática es más estricta: solo entra al contexto lo claramente relevante.
KNOWLEDGE_PREFETCH_THRESHOLD = float(os.getenv("KNOWLEDGE_PREFETCH_THRESHOLD", "0.45"))
KNOWLEDGE_PREFETCH_MAX_RESULTS = int(os.getenv("KNOWLEDGE_PREFETCH_MAX_RESULTS", "4"))
KNOWLEDGE_MATCH_MAX_CHARS = int(os.getenv("KNOWLEDGE_MATCH_MAX_CHARS", "1800"))
# Cada cuántos minutos se revisa Knowledge y se suben solo los cambios. 0 lo desactiva.
KNOWLEDGE_SYNC_INTERVAL_MINUTES = float(os.getenv("KNOWLEDGE_SYNC_INTERVAL_MINUTES", "15"))

# Los modelos de razonamiento gastan tokens de salida pensando; 700 cortaba respuestas.
OPENAI_MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "2500"))
OPENAI_REASONING_EFFORT = (os.getenv("OPENAI_REASONING_EFFORT", "low") or "").strip().lower()
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "25"))
ODOO_TIMEOUT = float(os.getenv("ODOO_TIMEOUT", "8"))

MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY", "")
MANYCHAT_API = "https://api.manychat.com"
# Etiqueta que se pone al contacto cuando la IA pide un asesor humano (modo /chat_async). Vacío = no etiquetar.
MC_HUMAN_TAG = os.getenv("MC_HUMAN_TAG", "")

# Nombres de los campos personalizados de ManyChat que usa el flujo "Agente Sporthouse".
MC_FIELDS = {
    "reply": os.getenv("MC_FIELD_REPLY", "ai_reply"),
    "school": os.getenv("MC_FIELD_SCHOOL", "escuela_usuario"),
    "order_number": os.getenv("MC_FIELD_ORDER", "odoo_order_number"),
    "intent": os.getenv("MC_FIELD_INTENT", "mc_intent"),
    "needs_human": os.getenv("MC_FIELD_NEEDS_HUMAN", "ai_needs_human"),
    "response_id": os.getenv("MC_FIELD_RESPONSE_ID", "ai_response_id"),
    "conversation_id": os.getenv("MC_FIELD_CONVERSATION_ID", "ai_conversation_id"),
}


# ----------------------------------------------------------------------
# Clientes compartidos (antes se creaban y autenticaban en cada mensaje)
# ----------------------------------------------------------------------

_OPENAI_CLIENT = None
_OPENAI_CLIENT_LOCK = threading.Lock()


def _openai_client() -> OpenAI:
    """Cliente único. Pocos reintentos: un 429 por falta de crédito no se arregla reintentando."""
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        with _OPENAI_CLIENT_LOCK:
            if _OPENAI_CLIENT is None:
                _OPENAI_CLIENT = OpenAI(
                    api_key=OPENAI_API_KEY,
                    timeout=OPENAI_TIMEOUT,
                    max_retries=1,
                )
    return _OPENAI_CLIENT


class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _odoo_proxy(path: str) -> xmlrpc.client.ServerProxy:
    """Proxy nuevo por llamada (no es thread-safe compartirlo), con límite de tiempo."""
    url = f"{ODOO_URL}{path}"
    transport = (
        _TimeoutSafeTransport(ODOO_TIMEOUT)
        if url.startswith("https://")
        else _TimeoutTransport(ODOO_TIMEOUT)
    )
    return xmlrpc.client.ServerProxy(url, transport=transport, allow_none=True)


_ODOO_UID_CACHE = {"uid": None, "ts": 0.0}
_ODOO_UID_TTL = 1800

_FIELDS_CACHE: dict = {}
_FIELDS_CACHE_TTL = 3600


def _is_placeholder(value) -> bool:
    """ManyChat manda el texto literal {{cuf_123}} cuando el campo está vacío."""
    return isinstance(value, str) and bool(re.fullmatch(r"\s*\{\{[^{}]*\}\}\s*", value))


class ChatRequest(BaseModel):
    message: Optional[str] = None
    contact_id: Optional[str] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None

    # Contexto durable que ManyChat puede guardar como custom fields.
    school: Optional[str] = None
    order_number: Optional[str] = None
    product: Optional[str] = None
    size: Optional[str] = None
    intent: Optional[str] = None

    # Conversación persistente de OpenAI. Este es el identificador principal de memoria.
    # ManyChat debe guardarlo y reenviarlo en cada turno.
    conversation_id: Optional[str] = None

    # Compatibilidad temporal con la versión anterior. Ya no es la memoria principal.
    previous_response_id: Optional[str] = None

    @field_validator("*", mode="before")
    @classmethod
    def _clean_manychat_value(cls, value):
        # Campos vacíos de ManyChat llegan como "{{cuf_XXXX}}"; los tratamos como vacíos.
        if _is_placeholder(value):
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


def _norm(value: Optional[str]) -> str:
    value = (value or "").strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", value)
        if unicodedata.category(c) != "Mn"
    )




_SCHOOL_STOPWORDS = {
    "de", "del", "la", "las", "el", "los", "y", "e", "campus",
    # Todos los sitios se llaman "Sporthouse <Escuela>": la marca no distingue escuelas.
    "sporthouse", "sport", "house",
}

# Sitios web que no son escuelas (se pueden ajustar con la variable SCHOOL_EXCLUDE, separadas por coma).
_SCHOOL_EXCLUDE = {
    _n.strip() for _n in os.getenv("SCHOOL_EXCLUDE", "targo,sporthouse mexico").split(",") if _n.strip()
}

# Carpetas/artículos de Knowledge que la IA nunca debe usar (contenido interno).
_KNOWLEDGE_EXCLUDE_PREFIXES = [
    _n.strip() for _n in os.getenv("KNOWLEDGE_EXCLUDE", "interno,manual de usuario,bienvenido").split(",") if _n.strip()
]


def _display_school(name: Optional[str]) -> str:
    """'Sporthouse Monteverde' -> 'Monteverde' para hablar con las mamás."""
    return re.sub(r"^\s*sport\s*house\s+", "", (name or "").strip(), flags=re.I) or (name or "")

_SCHOOL_REGISTRY_CACHE = {"ts": 0.0, "items": []}
_SCHOOL_REGISTRY_TTL = 300


def _school_tokens(value: Optional[str]):
    norm = _norm(value)
    return [
        token for token in re.findall(r"[a-z0-9]+", norm)
        if token and token not in _SCHOOL_STOPWORDS
    ]


def _school_match_score(query: Optional[str], candidate: Optional[str]) -> float:
    """Puntaje genérico para equivalencias de escuela, incluyendo siglas."""
    q = _norm(query)
    c = _norm(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 100.0
    if q in c or c in q:
        return 92.0

    qt = _school_tokens(query)
    ct = _school_tokens(candidate)
    if not qt or not ct:
        return 0.0

    q_compact = "".join(qt)
    c_compact = "".join(ct)
    q_acronym = "".join(t[0] for t in qt if t)
    c_acronym = "".join(t[0] for t in ct if t)

    # Ej.: IMS <-> Instituto Mexico Secundaria.
    if q_compact == c_acronym or c_compact == q_acronym:
        return 90.0
    if len(q_compact) <= 6 and q_compact == c_acronym:
        return 90.0
    if len(c_compact) <= 6 and c_compact == q_acronym:
        return 90.0

    qs, cs = set(qt), set(ct)
    inter = len(qs & cs)
    if not inter:
        return 0.0

    containment = inter / min(len(qs), len(cs))
    jaccard = inter / len(qs | cs)
    return 70.0 * containment + 25.0 * jaccard


def _field_value_label(meta: dict, value):
    """Convierte selection/many2one/char a una etiqueta humana cuando sea posible."""
    if value in (None, False, ""):
        return None

    field_type = (meta or {}).get("type")
    if field_type == "many2one":
        if isinstance(value, (list, tuple)) and len(value) > 1:
            return str(value[1]).strip() or None
        return str(value).strip() or None

    if field_type == "selection":
        raw = str(value).strip()
        for option in (meta or {}).get("selection") or []:
            if isinstance(option, (list, tuple)) and len(option) >= 2 and str(option[0]) == raw:
                return str(option[1]).strip() or raw
        return raw or None

    if isinstance(value, str):
        return value.strip() or None

    return str(value).strip() or None


def _sale_order_school_fields(uid, models):
    """
    Descubre campos Studio del pedido que parecen identificar una escuela concreta.
    No codifica escuelas individuales: usa el nombre visible del campo (p. ej. "Escuela Marista").
    """
    try:
        fields = _fields_get(uid, models, "sale.order", ["type", "string", "selection"])
    except Exception:
        logger.warning("No se pudieron descubrir campos de escuela en sale.order", exc_info=True)
        return {}

    result = {}
    for name, meta in fields.items():
        if not name.startswith("x_studio_"):
            continue
        label = _norm((meta or {}).get("string"))
        if not any(word in label for word in ("escuela", "school", "colegio")):
            continue
        if (meta or {}).get("type") not in ("selection", "char", "many2one"):
            continue
        result[name] = meta
    return result


def _best_parent_website_for_field(field_label: str, websites: list):
    """Relaciona genéricamente un campo como 'Escuela Marista' con el website 'Maristas'."""
    label_tokens = [
        t for t in _school_tokens(field_label)
        if t not in ("escuela", "school", "colegio")
    ]
    label = " ".join(label_tokens)
    if not label:
        return None

    scored = []
    for website in websites:
        score = _school_match_score(label, website.get("name"))
        if score > 0:
            scored.append((score, website))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    score, website = scored[0]
    return website if score >= 55 else None


def _school_registry(uid, models):
    """
    Construye un catálogo de escuelas resolubles:
    - websites directos (EDRON, Eton, Maristas, etc.)
    - escuelas específicas contenidas en grupos, descubiertas desde campos Studio de sale.order.

    Así 'IMS' puede resolver a 'Instituto México Secundaria' y conservar website='Maristas'.
    """
    import time
    now = time.time()
    if _SCHOOL_REGISTRY_CACHE["items"] and now - _SCHOOL_REGISTRY_CACHE["ts"] < _SCHOOL_REGISTRY_TTL:
        return _SCHOOL_REGISTRY_CACHE["items"]

    website_fields = _available_fields(uid, models, "website")
    website_read_fields = [f for f in ("id", "name", "domain") if f in website_fields or f in ("id", "name")]
    websites = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "website", "search_read", [[]],
        {"fields": website_read_fields, "limit": 250, "order": "name asc"},
    )

    websites = [
        w for w in websites
        if " ".join(re.findall(r"[a-z0-9]+", _norm(w.get("name")))) not in _SCHOOL_EXCLUDE
    ]

    items = []
    for website in websites:
        items.append({
            "canonical_school": (website.get("name") or "").strip(),
            "website_id": website.get("id"),
            "domain": website.get("domain"),
            "parent_website": None,
            "source": "website",
            "source_field": None,
        })

    school_fields = _sale_order_school_fields(uid, models)
    for field_name, meta in school_fields.items():
        parent = _best_parent_website_for_field((meta or {}).get("string") or field_name, websites)
        if not parent:
            continue

        # Los selection son ideales porque permiten descubrir escuelas sin leer miles de pedidos.
        if (meta or {}).get("type") == "selection":
            for option in (meta or {}).get("selection") or []:
                if not isinstance(option, (list, tuple)) or len(option) < 2:
                    continue
                label = str(option[1]).strip()
                if not label:
                    continue
                items.append({
                    "canonical_school": label,
                    "website_id": parent.get("id"),
                    "domain": parent.get("domain"),
                    "parent_website": parent.get("name"),
                    "source": "sale_order_school_field",
                    "source_field": field_name,
                })

    # Deduplicar conservando la variante más específica si coincide el nombre.
    dedup = {}
    for item in items:
        key = _norm(item.get("canonical_school"))
        if not key:
            continue
        current = dedup.get(key)
        if current is None or (current.get("source") == "website" and item.get("source") != "website"):
            dedup[key] = item

    result = list(dedup.values())
    _SCHOOL_REGISTRY_CACHE.update({"ts": now, "items": result})
    return result


def _compact(value: Optional[str]) -> str:
    return "".join(_school_tokens(value))


def _fuzzy_school_score(query: Optional[str], candidate: Optional[str]) -> float:
    """
    Parecido aproximado (0-100) para nombres escritos con errores: "monte berde", "edrom",
    "maristaz", "instituto mexico sec". Tolera letras cambiadas, faltantes, espacios y acentos.
    """
    q, c = _compact(query), _compact(candidate)
    if len(q) < 3 or not c:
        return 0.0
    whole = SequenceMatcher(None, q, c).ratio()
    # Escribió solo el inicio del nombre ("montever", "institutomex").
    prefix = SequenceMatcher(None, q, c[: len(q)]).ratio() * 0.95 if len(q) >= 4 else 0.0
    # Palabra por palabra: cada palabra de la mamá contra la más parecida del nombre real.
    q_tokens, c_tokens = _school_tokens(query), _school_tokens(candidate)
    token_avg = 0.0
    if q_tokens and c_tokens:
        token_avg = sum(
            max(SequenceMatcher(None, qt, ct).ratio() for ct in c_tokens) for qt in q_tokens
        ) / len(q_tokens)
        # Penaliza cuando el nombre real tiene muchas más palabras de las que se parecen.
        token_avg *= min(1.0, (len(q_tokens) + 1) / (len(c_tokens) + 1) + 0.35)
    score = 100.0 * max(whole, prefix, token_avg)
    # Nombres cortos ("Eton", "EDRON"): una letra mal ya baja mucho el porcentaje,
    # así que contamos errores de dedo directamente.
    edits = _edit_distance(q, c)
    if edits <= 1 and len(c) >= 4:
        score = max(score, 88.0)
    elif edits <= 2 and len(c) >= 8:
        score = max(score, 82.0)
    return round(score, 1)


def _edit_distance(a: str, b: str) -> int:
    """Distancia de Levenshtein (letras cambiadas, faltantes o de más)."""
    if abs(len(a) - len(b)) > 3:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _school_names(uid, models) -> list[str]:
    try:
        return sorted(
            {
                _display_school(item.get("canonical_school"))
                for item in _school_registry(uid, models)
                if item.get("canonical_school")
            },
            key=_norm,
        )
    except Exception:
        logger.warning("No se pudo leer la lista de escuelas", exc_info=True)
        return []


def _resolve_school(uid, models, school: str):
    """
    Resuelve lo que escribió el cliente contra escuelas reales y grupos/sitios de Odoo.
    Una escuela puede vivir dentro de un website de grupo; canonical_school y parent_website son distintos.

    Si no hay coincidencia segura pero sí una parecida (mamá escribió con errores), regresa
    status=confirm_school con la sugerencia para que la IA pregunte UNA vez "¿Te refieres a X?".
    """
    school = (school or "").strip()
    if not school:
        return {"status": "missing_school"}

    registry = _school_registry(uid, models)

    def _ok(item, score, matched_by="nombre"):
        return {
            "status": "ok",
            "input": school,
            "canonical_school": item.get("canonical_school"),
            "website_id": item.get("website_id"),
            "domain": item.get("domain"),
            "parent_website": item.get("parent_website"),
            "source": item.get("source"),
            "source_field": item.get("source_field"),
            "score": round(score, 1),
            "matched_by": matched_by,
        }

    scored = []
    for item in registry:
        score = _school_match_score(school, item.get("canonical_school"))
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda item: (-item[0], _norm(item[1].get("canonical_school"))))

    if scored:
        best_score, best = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        # Solo el nombre exacto gana sin importar empates; "instituto mexico" no debe elegir
        # al azar entre Primaria y Secundaria.
        if best_score >= 58 and not (second_score >= best_score - 4 and best_score < 100):
            return _ok(best, best_score)

    # Sin coincidencia segura: búsqueda aproximada para errores de dedo.
    fuzzy = sorted(
        (
            (_fuzzy_school_score(school, item.get("canonical_school")), item)
            for item in registry
            if item.get("canonical_school")
        ),
        key=lambda x: (-x[0], _norm(x[1].get("canonical_school"))),
    )
    fuzzy = [(score, item) for score, item in fuzzy if score >= 60]

    if fuzzy:
        f_best, f_item = fuzzy[0]
        f_second = fuzzy[1][0] if len(fuzzy) > 1 else 0.0
        # Una sola escuela parecida (aunque el parecido sea medio) también se confirma directo.
        if (f_best >= 78 and f_best - f_second >= 6) or (f_best >= 70 and f_second == 0.0):
            return {
                "status": "confirm_school",
                "input": school,
                "suggestion": _display_school(f_item.get("canonical_school")),
                "group": f_item.get("parent_website"),
                "score": f_best,
                "message": (
                    f"Probablemente quiso decir \"{_display_school(f_item.get('canonical_school'))}\". Pregunta UNA sola vez "
                    "si se refiere a esa escuela. Si confirma, vuelve a llamar la herramienta con ese nombre exacto."
                ),
            }

    candidates = []
    seen = set()
    for score, item in (fuzzy + scored):
        name = _display_school(item.get("canonical_school"))
        if name and name not in seen:
            seen.add(name)
            candidates.append({
                "name": name,
                "group": _display_school(item.get("parent_website")) or None,
                "score": round(score, 1),
            })
    if candidates:
        return {
            "status": "ambiguous",
            "input": school,
            "candidates": candidates[:5],
            "message": "Puede ser más de una escuela. Pregunta UNA vez cuál de estas es, mostrando las opciones.",
        }

    return {
        "status": "not_found",
        "input": school,
        "available_schools": _school_names(uid, models)[:40],
        "message": "No se parece a ninguna escuela configurada. Pregunta cuál es, mostrando las escuelas disponibles.",
    }


def _order_specific_schools(order: dict, field_meta: dict):
    """Extrae las escuelas específicas guardadas en un pedido (p. ej. x_studio_marista)."""
    values = []
    for field_name, meta in field_meta.items():
        label = _field_value_label(meta, order.get(field_name))
        if not label:
            continue
        values.append({
            "field": field_name,
            "field_label": (meta or {}).get("string") or field_name,
            "school": label,
        })
    return values

def _clean_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<\s*br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _odoo():
    missing = [
        key for key, value in {
            "ODOO_URL": ODOO_URL,
            "ODOO_DB": ODOO_DB,
            "ODOO_USER": ODOO_USER,
            "ODOO_PASSWORD": ODOO_PASSWORD,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError(f"Faltan variables de Odoo: {', '.join(missing)}")

    # El uid no cambia: antes se iniciaba sesión en Odoo en cada herramienta de cada mensaje.
    now = time.time()
    uid = _ODOO_UID_CACHE.get("uid")
    if not uid or now - _ODOO_UID_CACHE.get("ts", 0.0) > _ODOO_UID_TTL:
        uid = _odoo_proxy("/xmlrpc/2/common").authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise RuntimeError("No se pudo autenticar con Odoo")
        _ODOO_UID_CACHE.update({"uid": uid, "ts": now})

    return uid, _odoo_proxy("/xmlrpc/2/object")


def _fields_get(uid, models, model_name: str, attributes: list[str]):
    """fields_get con caché: la estructura de los modelos casi nunca cambia."""
    key = (model_name, tuple(attributes))
    cached = _FIELDS_CACHE.get(key)
    now = time.time()
    if cached and now - cached["ts"] < _FIELDS_CACHE_TTL:
        return cached["fields"]
    fields = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        model_name, "fields_get", [],
        {"attributes": attributes},
    ) or {}
    _FIELDS_CACHE[key] = {"ts": now, "fields": fields}
    return fields


def _phone10(value) -> str:
    """Últimos 10 dígitos de un teléfono (ignora +52, 1, espacios y guiones)."""
    if not value or _is_placeholder(value):
        return ""
    digits = re.sub(r"\D", "", str(value))
    return digits[-10:] if len(digits) >= 10 else ""


def _manychat_phone(contact_id) -> str:
    """Teléfono de WhatsApp del contacto según ManyChat (si ManyChat no lo mandó en la petición)."""
    if not contact_id or not MANYCHAT_API_KEY or not str(contact_id).isdigit():
        return ""
    try:
        response = requests.get(
            f"{MANYCHAT_API}/fb/subscriber/getInfo",
            params={"subscriber_id": contact_id},
            headers={"Authorization": f"Bearer {MANYCHAT_API_KEY}"},
            timeout=8,
        )
        info = (response.json() or {}).get("data") or {}
        return info.get("whatsapp_phone") or info.get("phone") or ""
    except Exception as exc:
        logger.warning("No se pudo leer el teléfono del contacto %s en ManyChat: %s", contact_id, exc)
        return ""


def _fmt_fecha(value) -> Optional[str]:
    """'2026-11-20 18:00:00' -> '20/11/2026'."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return None


_LUGAR_ENTREGA = {"domicilio": "a domicilio", "samara": "en Samara", "colegio": "en el colegio"}


def consultar_pedido(school: str, order_number: str, phone: Optional[str] = None):
    """
    Consulta un pedido real y valida la escuela concreta del pedido.

    Importante: una escuela puede vivir dentro de un website/grupo (ej. Maristas).
    Si el pedido tiene un campo Studio de escuela específica, ese dato tiene prioridad
    sobre website_id para validar la escuela del cliente.
    """
    school = (school or "").strip()
    order_number = re.sub(r"[\s#\-_.]", "", (order_number or "").upper())
    # "S413" / "s 2038" / "S002038" -> S00413 / S02038. Solo se rellenan ceros a la izquierda.
    match = re.fullmatch(r"S(\d{1,7})", order_number)
    if match:
        digits = match.group(1).lstrip("0") or "0"
        order_number = "S" + digits.zfill(5)

    if not re.fullmatch(r"S\d{5,7}", order_number):
        return {
            "status": "invalid_format",
            "received": order_number,
            "expected_format": "S00000",
            "message": "El número todavía no está en formato canónico. No adivines si hay ambigüedad.",
        }

    uid, models = _odoo()

    # Descubrimos campos Studio que identifican escuela dentro de un grupo/sitio.
    school_field_meta = _sale_order_school_fields(uid, models)
    order_fields = ["id", "name", "website_id", "partner_id", "partner_shipping_id", "state",
                    "access_token", "commitment_date"] + list(school_field_meta.keys())

    orders = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search_read",
        [[['name', '=', order_number]]],
        {
            "fields": order_fields,
            "limit": 1,
        },
    )

    if not orders:
        return {
            "status": "not_found",
            "order_number": order_number,
            "message": "No existe un pedido con ese número exacto. Pide al cliente verificarlo; no propongas otro número.",
        }

    order = orders[0]
    website = order.get("website_id")
    website_id = website[0] if isinstance(website, (list, tuple)) and website else None
    website_name = website[1] if isinstance(website, (list, tuple)) and len(website) > 1 else ""

    specific_schools = _order_specific_schools(order, school_field_meta)

    # Si la mamá escribe desde el mismo número que tiene el pedido, no hace falta verificar la escuela.
    phone_verified = False
    caller = _phone10(phone)
    if caller:
        partner_ids = list({
            v[0] for v in (order.get("partner_id"), order.get("partner_shipping_id"))
            if isinstance(v, (list, tuple)) and v
        })
        if partner_ids:
            partners = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "res.partner", "read",
                [partner_ids], {"fields": ["phone"]},
            ) or []
            phone_verified = any(_phone10(p.get("phone")) == caller for p in partners)

    if phone_verified:
        canonical_school = (specific_schools[0].get("school") if specific_schools else None) \
            or _display_school(website_name) or website_name or None
        school_source_field = specific_schools[0].get("field") if specific_schools else None
        return _resumen_pedido(uid, models, order, order_number, canonical_school,
                               website_name, school_source_field, verified_by="phone")

    if not school:
        return {
            "status": "missing_school",
            "order_number": order_number,
            "message": (
                "El número de WhatsApp no coincide con el del pedido. Para proteger los datos, "
                "pregunta de qué escuela es el pedido antes de revelar cualquier información."
            ),
        }

    # Primero resolvemos lo que escribió el cliente. Esto entiende siglas como IMS y
    # puede devolver canonical_school=Instituto México Secundaria, parent_website=Maristas.
    resolved_school = _resolve_school(uid, models, school)

    # Si el pedido tiene escuela específica, ese dato es la fuente de verdad.
    # No basta con decir solo el grupo (Maristas) porque puede contener varias escuelas.
    if specific_schools:
        scored = []
        for item in specific_schools:
            score = _school_match_score(school, item.get("school"))
            if resolved_school.get("status") == "ok":
                score = max(
                    score,
                    _school_match_score(resolved_school.get("canonical_school"), item.get("school")),
                )
            scored.append((score, item))

        scored.sort(key=lambda x: -x[0])
        best_score, best_specific = scored[0]

        # Caso especial conceptual, no por escuela: el usuario dio el grupo/sitio, pero
        # el pedido tiene una escuela más específica. Pedimos la escuela concreta.
        group_score = _school_match_score(school, website_name)
        if best_score < 58 and group_score >= 85:
            return {
                "status": "specific_school_required",
                "order_number": order_number,
                "website_group": website_name or None,
                "message": "El pedido pertenece a un grupo que contiene varias escuelas. Pide la escuela específica antes de revelar el estatus.",
            }

        if best_score < 58 and resolved_school.get("status") != "ok":
            # Escuela mal escrita o dudosa: primero se confirma, no se declara "no corresponde".
            return {
                "status": "school_unresolved",
                "school_input": school,
                "school_resolution": resolved_school,
                "message": "Confirma la escuela con el cliente (una sola vez) antes de consultar el pedido.",
            }

        if best_score < 58:
            return {
                "status": "school_mismatch",
                "order_number": order_number,
                "school_input": school,
                "website_group": website_name or None,
                "message": "El pedido no corresponde a la escuela indicada. No reveles información del pedido.",
            }

        canonical_school = best_specific.get("school")
        school_source_field = best_specific.get("field")

    else:
        # Pedido sin escuela específica: website_id sí funciona como frontera escolar.
        if resolved_school.get("status") != "ok":
            return {
                "status": "school_unresolved",
                "school_input": school,
                "school_resolution": resolved_school,
                "message": "No pude identificar de forma segura la escuela. Pide una aclaración antes de consultar el pedido.",
            }

        canonical_school = resolved_school.get("canonical_school") or school
        canonical_website_id = resolved_school.get("website_id")
        if not website_id or (canonical_website_id and website_id != canonical_website_id):
            return {
                "status": "school_mismatch",
                "order_number": order_number,
                "school": canonical_school,
                "website_group": website_name or None,
                "message": "El pedido no corresponde a la escuela indicada. No reveles información del pedido.",
            }
        school_source_field = None

    return _resumen_pedido(uid, models, order, order_number, canonical_school,
                           website_name, school_source_field, verified_by="school")


def _resumen_pedido(uid, models, order, order_number, canonical_school, website_name,
                    school_source_field, verified_by):
    """Resumen claro del pedido: estado general, paquetes, fechas y liga de detalle."""
    picking_meta = _fields_get(uid, models, "stock.picking", ["type", "string", "selection"])
    status_field = "x_studio_estado_sporthouse"
    picking_fields = [
        f for f in ("id", "name", "state", status_field, "picking_type_code", "date_done",
                    "scheduled_date", "backorder_id")
        if f in picking_meta
    ]
    # sale_id liga la entrega al pedido aunque el "origen" se haya editado; origin queda como respaldo.
    if "sale_id" in picking_meta:
        picking_domain = ["|", ["sale_id", "=", order["id"]], ["origin", "=", order_number]]
    else:
        picking_domain = [["origin", "=", order_number]]

    pickings = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "stock.picking", "search_read",
        [picking_domain],
        {"fields": picking_fields, "limit": 30, "order": "id"},
    ) or []

    def _status_label(row):
        # Si el campo es de selección, Odoo regresa la clave interna; usamos la etiqueta visible.
        return _field_value_label(picking_meta.get(status_field) or {}, row.get(status_field))

    # Paquetes = entregas al cliente no canceladas (una por cada envío parcial).
    paquetes_raw = [p for p in pickings if p.get("picking_type_code") == "outgoing" and p.get("state") != "cancel"]
    if not paquetes_raw:
        paquetes_raw = [p for p in pickings if p.get("state") != "cancel" and _status_label(p)]

    ids = [p["id"] for p in paquetes_raw]
    productos, lugares = {}, {}
    if ids:
        try:
            moves = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "stock.move", "search_read",
                [[["picking_id", "in", ids], ["state", "!=", "cancel"]]],
                {"fields": ["picking_id", "product_id", "product_uom_qty"], "limit": 200},
            ) or []
            for m in moves:
                nombre = (m.get("product_id") or [None, ""])[1]
                nombre = re.sub(r"^\[[^\]]*\]\s*", "", nombre or "")
                qty = m.get("product_uom_qty") or 0
                productos.setdefault(m["picking_id"][0], []).append(
                    f"{int(qty) if float(qty).is_integer() else qty} × {nombre}"
                )
        except Exception as exc:
            logger.warning("No se pudieron leer los productos de %s: %s", order_number, exc)
        try:
            escaneos = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "x_escaneo_salida", "search_read",
                [[["x_picking_id", "in", ids], ["x_actividad", "in", list(_LUGAR_ENTREGA)]]],
                {"fields": ["x_picking_id", "x_actividad"], "order": "id"},
            ) or []
            for e in escaneos:
                lugares[e["x_picking_id"][0]] = _LUGAR_ENTREGA.get(e.get("x_actividad"))
        except Exception as exc:
            logger.warning("No se pudo leer dónde se entregó %s: %s", order_number, exc)

    paquetes = []
    for i, p in enumerate(paquetes_raw, 1):
        entregado = p.get("state") == "done"
        estado = _status_label(p) or ("Entregado" if entregado else "En preparación")
        item = {
            "paquete": i,
            "entrega": p.get("name"),
            "sporthouse_status": estado,
            "entregado": entregado,
            "productos": (productos.get(p["id"]) or [])[:8],
        }
        if entregado:
            item["fecha_entrega"] = _fmt_fecha(p.get("date_done"))
            item["lugar_entrega"] = lugares.get(p["id"])
        else:
            item["fecha_estimada"] = _fmt_fecha(p.get("scheduled_date"))
        paquetes.append(item)

    total = len(paquetes)
    entregados = sum(1 for p in paquetes if p["entregado"])
    if not total:
        resumen = "Pedido recibido, todavía sin entregas registradas"
    elif entregados == total:
        resumen = "Entregado" if total == 1 else f"Entregado completo ({total} paquetes)"
    elif entregados:
        resumen = f"Entrega parcial · {entregados} de {total} paquetes entregados"
    else:
        resumen = paquetes[-1]["sporthouse_status"] if total == 1 else f"En proceso · {total} paquetes"

    # Liga de detalle en la tienda del pedido. Con el número verificado va directo al detalle
    # completo (portal); si no, a la página pública de rastreo, que no muestra datos personales.
    website_id = (order.get("website_id") or [None])[0]
    base = None
    if website_id:
        try:
            site = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "website", "read",
                [[website_id]], {"fields": ["domain"]},
            ) or []
            base = (site[0].get("domain") or None) if site else None
        except Exception:
            base = None
    token = order.get("access_token")
    if verified_by == "phone" and not token:
        # Igual que _portal_ensure_token de Odoo: el pedido nunca se ha abierto en el portal.
        try:
            token = str(uuid.uuid4())
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "write",
                              [[order["id"]], {"access_token": token}])
        except Exception as exc:
            logger.warning("No se pudo crear la liga segura de %s: %s", order_number, exc)
            token = None
    if verified_by == "phone" and token:
        order = dict(order, access_token=token)
        detail_path = f"/my/orders/{order['id']}?access_token={order['access_token']}"
    else:
        detail_path = f"/rastreo?pedido={order_number}"

    result = {
        "status": "ok",
        "verified_by": verified_by,
        "order_number": order_number,
        "school": canonical_school,
        "website_group": website_name or None,
        "school_source_field": school_source_field,
        "resumen": resumen,
        "paquetes": paquetes,
        "fecha_estimada_pedido": _fmt_fecha(order.get("commitment_date")),
        "detail_url": _absolute_website_url(base, detail_path),
        # Compatibilidad con el formato anterior.
        "sporthouse_status": paquetes[-1]["sporthouse_status"] if paquetes else None,
        "internal_delivery_state": paquetes_raw[-1].get("state") if paquetes_raw else None,
    }
    if not paquetes:
        result["note"] = "El pedido existe pero todavía no tiene entregas registradas."
    return result



_KNOWLEDGE_ARTICLE_CACHE = {"ts": 0.0, "items": []}
_KNOWLEDGE_ARTICLE_CACHE_TTL = 120
_VECTOR_STORE_CACHE = {"ts": 0.0, "id": None}
_VECTOR_STORE_CACHE_TTL = 300

_KNOWLEDGE_SYNC_LOCK = threading.Lock()
_KNOWLEDGE_SYNC_STATE = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "message": None,
    "result": None,
    "error": None,
}


def _load_knowledge_articles(uid, models, force: bool = False):
    """Lee Knowledge de Odoo. Odoo sigue siendo la fuente maestra."""
    now = time.time()
    if (
        not force
        and _KNOWLEDGE_ARTICLE_CACHE["items"]
        and now - _KNOWLEDGE_ARTICLE_CACHE["ts"] < _KNOWLEDGE_ARTICLE_CACHE_TTL
    ):
        return _KNOWLEDGE_ARTICLE_CACHE["items"]

    fields_meta = _available_fields(uid, models, "knowledge.article")
    read_fields = ["id", "name", "body"]
    for optional in ("write_date", "parent_id"):
        if optional in fields_meta:
            read_fields.append(optional)

    # Solo contenido pensado para compartirse: fuera artículos privados del usuario API y plantillas.
    domain = []
    if "category" in fields_meta:
        domain.append(["category", "!=", "private"])
    if "is_template" in fields_meta:
        domain.append(["is_template", "=", False])
    if "to_delete" in fields_meta:
        domain.append(["to_delete", "=", False])

    def _read(dom):
        return models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "knowledge.article", "search_read",
            [dom],
            {
                "fields": read_fields,
                "order": "name asc",
                "limit": 3000,
            },
        ) or []

    rows = _read(domain)
    if not rows and domain:
        # Si todo el contenido vive en la sección privada del usuario API, no lo perdemos.
        logger.warning("Knowledge: el filtro de artículos privados/plantillas dejó 0 artículos; se usan todos")
        rows = _read([])

    cleaned = []
    for row in rows:
        text = _clean_html(row.get("body") or "")
        name = (row.get("name") or "").strip()
        if not name and not text:
            continue
        parent = row.get("parent_id")
        cleaned.append({
            "id": row.get("id"),
            "name": name or f"Artículo {row.get('id')}",
            "text": text,
            "write_date": row.get("write_date") or "",
            "parent_id": parent[0] if isinstance(parent, (list, tuple)) and parent else None,
        })

    _KNOWLEDGE_ARTICLE_CACHE.update({"ts": now, "items": cleaned})
    return cleaned


def _norm_key(value: Optional[str]) -> str:
    value = _norm(value)
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value[:240]


def _knowledge_groups(registry: list[dict]) -> list[str]:
    groups = []
    for item in registry:
        value = (item.get("parent_website") or "").strip()
        if value and not any(_norm(value) == _norm(x) for x in groups):
            groups.append(value)
    return groups


def _scope_score(alias: str, article_name: str, article_text: str, distinctive: Optional[set] = None) -> float:
    """Puntaje genérico: prioriza el título y usa el cuerpo solo como fallback."""
    if not alias:
        return 0.0
    score = _school_match_score(alias, article_name)
    alias_norm = _norm(alias)
    title_norm = _norm(article_name)
    body_norm = _norm((article_text or "")[:2500])
    display_norm = _norm(_display_school(alias))
    if alias_norm and alias_norm in title_norm:
        score = max(score, 98.0)
    elif display_norm and re.search(rf"\b{re.escape(display_norm)}\b", title_norm):
        # "Compra Edron" -> Sporthouse Edron; "Horarios José Vasconcelos" -> Sporthouse José Vasconcelos.
        score = max(score, 95.0)
    elif alias_norm and alias_norm in body_norm:
        score = max(score, 55.0)
    # Una palabra que solo existe en el nombre de ESA escuela ("Vasconcelos", "Covadonga") basta.
    if distinctive:
        title_tokens = set(_school_tokens(article_name))
        hits = [t for t in _school_tokens(alias) if t in distinctive and t in title_tokens]
        if hits:
            score = max(score, 88.0)
    return score


def _distinctive_tokens(registry: list[dict]) -> set:
    """Palabras (4+ letras) que aparecen en el nombre de una sola escuela/grupo."""
    counts: dict = {}
    names = {item.get("canonical_school") for item in registry} | {item.get("parent_website") for item in registry}
    for name in names:
        for token in set(_school_tokens(name)):
            counts[token] = counts.get(token, 0) + 1
    return {t for t, n in counts.items() if n == 1 and len(t) >= 4}


def _infer_article_scope(article: dict, registry: list[dict]) -> dict:
    """
    Infere el alcance de un artículo sin hardcodear escuelas ni temas.
    - Si el título apunta a una escuela concreta -> scope_type=school.
    - Si apunta a un website que agrupa escuelas -> scope_type=group.
    - Si no hay alcance claro -> global.
    """
    groups = _knowledge_groups(registry)
    group_norms = {_norm(g) for g in groups}
    distinctive = _distinctive_tokens(registry)

    best_school = (0.0, None)
    best_group = (0.0, None)

    for group in groups:
        score = _scope_score(group, article.get("name") or "", article.get("text") or "", distinctive)
        if score > best_group[0]:
            best_group = (score, group)

    for item in registry:
        name = (item.get("canonical_school") or "").strip()
        if not name:
            continue
        # Un website que es padre de otras escuelas se trata como grupo, no como escuela concreta.
        if item.get("source") == "website" and _norm(name) in group_norms:
            continue
        score = _scope_score(name, article.get("name") or "", article.get("text") or "", distinctive)
        if score > best_school[0]:
            best_school = (score, name)

    school_score, school_name = best_school
    group_score, group_name = best_group

    if school_name and school_score >= 68 and school_score >= group_score + 4:
        return {
            "scope_type": "school",
            "scope_name": school_name,
            "scope_key": _norm_key(school_name),
            "score": round(school_score, 1),
        }

    if group_name and group_score >= 65:
        return {
            "scope_type": "group",
            "scope_name": group_name,
            "scope_key": _norm_key(group_name),
            "score": round(group_score, 1),
        }

    if school_name and school_score >= 60:
        return {
            "scope_type": "school",
            "scope_name": school_name,
            "scope_key": _norm_key(school_name),
            "score": round(school_score, 1),
        }

    return {
        "scope_type": "global",
        "scope_name": "SportHouse",
        "scope_key": "global",
        "score": 0.0,
    }


def _infer_scope_with_folders(article: dict, by_id: dict, registry: list[dict]) -> dict:
    """
    Carpetas por escuela: si el artículo no dice su escuela en el título, la hereda de la
    página padre (o abuela, etc.). Así basta con crear en Knowledge una página "Monteverde"
    y meter adentro todos sus artículos.
    """
    own = _infer_article_scope(article, registry)
    if own.get("scope_type") != "global":
        return own

    seen = {article.get("id")}
    parent = by_id.get(article.get("parent_id"))
    while parent and parent.get("id") not in seen and len(seen) < 12:
        seen.add(parent.get("id"))
        # Para las carpetas solo cuenta el título (el cuerpo de la carpeta puede mencionar varias escuelas).
        inherited = _infer_article_scope({"name": parent.get("name"), "text": ""}, registry)
        if inherited.get("scope_type") != "global":
            return {**inherited, "inherited_from": parent.get("name")}
        parent = by_id.get(parent.get("parent_id"))
    return own


def _get_vector_store_id(client: OpenAI, create_if_missing: bool = True) -> Optional[str]:
    """Usa ID fijo si existe; si no, localiza/crea el store por nombre."""
    if OPENAI_KNOWLEDGE_VECTOR_STORE_ID:
        return OPENAI_KNOWLEDGE_VECTOR_STORE_ID

    now = time.time()
    if _VECTOR_STORE_CACHE.get("id") and now - _VECTOR_STORE_CACHE.get("ts", 0) < _VECTOR_STORE_CACHE_TTL:
        return _VECTOR_STORE_CACHE["id"]

    try:
        page = client.vector_stores.list(limit=100)
        for store in getattr(page, "data", []) or []:
            if (getattr(store, "name", "") or "").strip() == OPENAI_KNOWLEDGE_VECTOR_STORE_NAME:
                _VECTOR_STORE_CACHE.update({"ts": now, "id": store.id})
                return store.id
    except Exception:
        logger.warning("No se pudo listar vector stores", exc_info=True)

    if not create_if_missing:
        return None

    store = client.vector_stores.create(name=OPENAI_KNOWLEDGE_VECTOR_STORE_NAME)
    _VECTOR_STORE_CACHE.update({"ts": now, "id": store.id})
    return store.id


def _list_vector_store_files(client: OpenAI, vector_store_id: str):
    items = []
    after = None
    while True:
        kwargs = {"vector_store_id": vector_store_id, "limit": 100}
        if after:
            kwargs["after"] = after
        page = client.vector_stores.files.list(**kwargs)
        data = list(getattr(page, "data", []) or [])
        items.extend(data)
        if not getattr(page, "has_more", False) or not data:
            break
        after = getattr(data[-1], "id", None)
        if not after:
            break
    return items


def _clear_vector_store(client: OpenAI, vector_store_id: str):
    deleted = 0
    for item in _list_vector_store_files(client, vector_store_id):
        file_id = getattr(item, "id", None)
        if not file_id:
            continue
        try:
            client.vector_stores.files.delete(
                vector_store_id=vector_store_id,
                file_id=file_id,
            )
            deleted += 1
        except Exception:
            logger.warning("No se pudo desvincular archivo %s", file_id, exc_info=True)
        # Eliminar también el archivo subyacente para no acumular almacenamiento.
        try:
            client.files.delete(file_id)
        except Exception:
            pass
    return deleted


def _article_file_content(article: dict, scope: dict) -> str:
    return (
        f"TÍTULO: {article.get('name') or ''}\n"
        f"ALCANCE: {scope.get('scope_type')} - {scope.get('scope_name')}\n"
        f"ARTÍCULO ODOO ID: {article.get('id')}\n"
        f"ÚLTIMA ACTUALIZACIÓN ODOO: {article.get('write_date') or 'N/D'}\n\n"
        f"CONTENIDO:\n{article.get('text') or ''}\n"
    )


def _article_is_usable(article: dict, by_id: dict) -> bool:
    """Fuera: artículos vacíos o casi vacíos (carpetas) y lo que viva en carpetas internas."""
    text = (article.get("text") or "").strip()
    if len(text) < 40 or _norm(text) == _norm(article.get("name")):
        return False
    node, seen = article, set()
    while node and node.get("id") not in seen and len(seen) < 12:
        seen.add(node.get("id"))
        name = _norm(node.get("name"))
        if any(name.startswith(_norm(prefix)) for prefix in _KNOWLEDGE_EXCLUDE_PREFIXES):
            return False
        node = by_id.get(node.get("parent_id"))
    return True


def _article_sync_sig(article: dict, scope: dict) -> str:
    """Huella de lo que se indexó: si cambia el artículo o su escuela, se vuelve a subir."""
    return f"{article.get('write_date') or ''}|{scope.get('scope_type')}|{scope.get('scope_key')}"[:500]


def _upload_article(client: OpenAI, vector_store_id: str, article: dict, scope: dict):
    safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", article.get("name") or "article")[:90]
    filename = f"odoo_{article.get('id')}_{safe_title}.txt"
    content = _article_file_content(article, scope)

    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            path = tmp.name

        with open(path, "rb") as fh:
            vector_file = client.vector_stores.files.upload_and_poll(
                vector_store_id=vector_store_id,
                file=fh,
            )

        file_id = getattr(vector_file, "id", None)
        if not file_id:
            raise RuntimeError("OpenAI no devolvió file_id al indexar artículo")

        attrs = {
            "scope_type": scope.get("scope_type") or "global",
            "scope_key": scope.get("scope_key") or "global",
            "scope_name": (scope.get("scope_name") or "SportHouse")[:256],
            "article_id": int(article.get("id") or 0),
            "article_name": (article.get("name") or "")[:256],
            "write_date": (article.get("write_date") or "")[:256],
            "source": "odoo_knowledge",
            "sync_sig": _article_sync_sig(article, scope),
        }
        client.vector_stores.files.update(
            vector_store_id=vector_store_id,
            file_id=file_id,
            attributes=attrs,
        )
        return file_id
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:
                pass


def sync_knowledge_vector_store():
    """
    Reconstruye el índice semántico desde Knowledge de Odoo.

    La sincronización es segura: primero sube los archivos nuevos y solo al final
    elimina los archivos anteriores. Así, si una carga falla a la mitad, el índice
    previo sigue disponible.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY no configurada")

    uid, models = _odoo()
    registry = _school_registry(uid, models)
    articles = _load_knowledge_articles(uid, models, force=True)
    if not articles:
        # Nunca vaciar el índice porque Odoo no devolvió nada (error de permisos, etc.).
        raise RuntimeError("Odoo no devolvió artículos de Knowledge; se conservó el índice anterior.")
    by_id = {a.get("id"): a for a in articles}
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=90.0)
    vector_store_id = _get_vector_store_id(client, create_if_missing=True)

    # Conservamos el índice anterior mientras construimos el nuevo.
    previous_files = [getattr(item, "id", None) for item in _list_vector_store_files(client, vector_store_id)]
    previous_files = [file_id for file_id in previous_files if file_id]

    indexed = []
    skipped = []

    total = len(articles)
    for pos, article in enumerate(articles, start=1):
        _KNOWLEDGE_SYNC_STATE["message"] = f"Indexando artículo {pos} de {total}: {article.get('name') or article.get('id')}"
        if not _article_is_usable(article, by_id):
            skipped.append({"id": article.get("id"), "name": article.get("name"), "reason": "vacío o interno"})
            continue
        scope = _infer_scope_with_folders(article, by_id, registry)
        try:
            file_id = _upload_article(client, vector_store_id, article, scope)
            indexed.append({
                "article_id": article.get("id"),
                "article_name": article.get("name"),
                "scope_type": scope.get("scope_type"),
                "scope_name": scope.get("scope_name"),
                "file_id": file_id,
            })
        except Exception as exc:
            logger.exception("No se pudo indexar Knowledge article %s", article.get("id"))
            skipped.append({
                "id": article.get("id"),
                "name": article.get("name"),
                "reason": str(exc)[:300],
            })

    # Si no logramos indexar nada, no tocamos el índice anterior.
    if not indexed and articles:
        raise RuntimeError(
            "No se pudo indexar ningún artículo. El índice anterior se conservó. "
            f"Primeros errores: {skipped[:3]}"
        )

    removed = 0
    for file_id in previous_files:
        # Si por alguna razón OpenAI reutilizara un ID, no lo eliminamos.
        if any(item.get("file_id") == file_id for item in indexed):
            continue
        try:
            client.vector_stores.files.delete(vector_store_id=vector_store_id, file_id=file_id)
            removed += 1
        except Exception:
            logger.warning("No se pudo desvincular archivo anterior %s", file_id, exc_info=True)
        try:
            client.files.delete(file_id)
        except Exception:
            pass

    return {
        "status": "ok",
        "vector_store_id": vector_store_id,
        "vector_store_name": OPENAI_KNOWLEDGE_VECTOR_STORE_NAME,
        "removed_previous_files": removed,
        "odoo_articles_found": len(articles),
        "indexed_count": len(indexed),
        "skipped_count": len(skipped),
        "indexed": indexed,
        "skipped": skipped,
    }


def sync_knowledge_changes():
    """
    Sincronización por cambios: solo sube los artículos nuevos o editados (o que cambiaron de
    escuela/carpeta) y quita los que se borraron en Odoo. Barata, se puede correr seguido.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY no configurada")

    uid, models = _odoo()
    registry = _school_registry(uid, models)
    articles = _load_knowledge_articles(uid, models, force=True)
    if not articles:
        raise RuntimeError("Odoo no devolvió artículos de Knowledge; no se tocó el índice.")
    by_id = {a.get("id"): a for a in articles}

    desired = {}
    for article in articles:
        if not _article_is_usable(article, by_id):
            continue
        scope = _infer_scope_with_folders(article, by_id, registry)
        desired[int(article.get("id") or 0)] = (article, scope, _article_sync_sig(article, scope))

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=90.0)
    vector_store_id = _get_vector_store_id(client, create_if_missing=True)

    existing = {}   # article_id -> [(file_id, sig)]
    orphans = []    # archivos sin artículo de Odoo asociado
    for item in _list_vector_store_files(client, vector_store_id):
        attrs = getattr(item, "attributes", None) or {}
        try:
            article_id = int(attrs.get("article_id") or 0)
        except (TypeError, ValueError):
            article_id = 0
        if not article_id:
            orphans.append(item.id)
            continue
        existing.setdefault(article_id, []).append((item.id, attrs.get("sync_sig")))

    def _drop(file_id):
        try:
            client.vector_stores.files.delete(vector_store_id=vector_store_id, file_id=file_id)
        except Exception:
            logger.warning("No se pudo desvincular archivo %s", file_id, exc_info=True)
        try:
            client.files.delete(file_id)
        except Exception:
            pass

    uploaded, updated, removed, failed = [], [], 0, []
    for article_id, (article, scope, sig) in desired.items():
        current = existing.get(article_id, [])
        if any(file_sig == sig for _, file_sig in current) and len(current) == 1:
            continue  # sin cambios
        keep = [fid for fid, file_sig in current if file_sig == sig][:1]
        try:
            if not keep:
                keep = [_upload_article(client, vector_store_id, article, scope)]
                (updated if current else uploaded).append(article.get("name"))
        except Exception as exc:
            logger.exception("No se pudo indexar Knowledge article %s", article_id)
            failed.append({"id": article_id, "name": article.get("name"), "reason": str(exc)[:200]})
            continue
        # Solo después de tener la versión nueva se borra la anterior.
        for fid, _ in current:
            if fid not in keep:
                _drop(fid)
                removed += 1

    gone = [aid for aid in existing if aid not in desired]
    gone_files = sum(len(existing[aid]) for aid in gone)
    total_files = sum(len(v) for v in existing.values())
    # Protección: si de golpe "desaparece" casi todo, probablemente es un error de lectura, no un borrado real.
    if total_files >= 6 and gone_files > 0.6 * total_files:
        logger.warning("Sync Knowledge: %s de %s archivos se borrarían; se omite por seguridad", gone_files, total_files)
    else:
        for aid in gone:
            for fid, _ in existing[aid]:
                _drop(fid)
                removed += 1
        for fid in orphans:
            _drop(fid)
            removed += 1

    return {
        "status": "ok",
        "mode": "cambios",
        "vector_store_id": vector_store_id,
        "odoo_articles_found": len(articles),
        "indexed_count": len(desired),
        "new": uploaded,
        "updated": updated,
        "removed_files": removed,
        "skipped_count": len(failed),
        "skipped": failed,
    }


def _run_knowledge_sync_job(full: bool = True):
    """Ejecuta la sincronización fuera de la petición HTTP para evitar timeouts del proxy."""
    acquired = _KNOWLEDGE_SYNC_LOCK.acquire(blocking=False)
    if not acquired:
        return
    try:
        _KNOWLEDGE_SYNC_STATE.update({
            "status": "running",
            "started_at": datetime.utcnow().isoformat() + "Z",
            "finished_at": None,
            "message": "Iniciando sincronización de Knowledge...",
            "result": None,
            "error": None,
        })
        result = sync_knowledge_vector_store() if full else sync_knowledge_changes()
        _KNOWLEDGE_SYNC_STATE.update({
            "status": "completed",
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "message": "Sincronización completada.",
            "result": result,
            "error": None,
        })
    except Exception as exc:
        logger.exception("Error en sincronización de Knowledge en background")
        _KNOWLEDGE_SYNC_STATE.update({
            "status": "failed",
            "finished_at": datetime.utcnow().isoformat() + "Z",
            "message": "La sincronización falló.",
            "result": None,
            "error": str(exc),
        })
    finally:
        _KNOWLEDGE_SYNC_LOCK.release()


def _knowledge_filter(resolved_school: dict):
    filters = [{"type": "eq", "key": "scope_type", "value": "global"}]
    school = (resolved_school.get("canonical_school") or "").strip()
    group = (resolved_school.get("parent_website") or "").strip()
    if school:
        filters.append({"type": "eq", "key": "scope_key", "value": _norm_key(school)})
    if group and _norm(group) != _norm(school):
        filters.append({"type": "eq", "key": "scope_key", "value": _norm_key(group)})
    return {"type": "or", "filters": filters}


def _result_text(result) -> str:
    chunks = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def buscar_info_escuela(
    school: str,
    query: str,
    score_threshold: Optional[float] = None,
    max_results: int = 8,
    rewrite_query: bool = True,
):
    """
    RAG persistente: busca por significado en el índice vectorial sincronizado desde Odoo.
    Filtra antes de buscar para no mezclar escuelas/grupos.
    """
    threshold = KNOWLEDGE_SCORE_THRESHOLD if score_threshold is None else score_threshold
    school = (school or "").strip()
    query = (query or "").strip()
    if not school:
        return {
            "status": "missing_school",
            "message": "Falta la escuela. Pregunta al cliente antes de buscar información específica.",
        }
    if not query:
        return {"status": "missing_query", "message": "Falta la consulta de conocimiento."}
    if not OPENAI_API_KEY:
        return {"status": "error", "message": "OpenAI no está configurado."}

    uid, models = _odoo()
    resolved_school = _resolve_school(uid, models, school)
    if resolved_school.get("status") != "ok":
        return {
            "status": "school_unresolved",
            "school_input": school,
            "school_resolution": resolved_school,
            "message": "No pude identificar de forma segura la escuela. Pide una aclaración.",
        }

    client = _openai_client()
    vector_store_id = _get_vector_store_id(client, create_if_missing=False)
    if not vector_store_id:
        return {
            "status": "knowledge_not_synced",
            "school": resolved_school.get("canonical_school") or school,
            "message": "La base semántica de Knowledge todavía no está sincronizada.",
        }

    results = client.vector_stores.search(
        vector_store_id=vector_store_id,
        query=query,
        filters=_knowledge_filter(resolved_school),
        rewrite_query=rewrite_query,
        max_num_results=max_results,
        ranking_options={"score_threshold": threshold},
    )

    matches = []
    for result in getattr(results, "data", []) or []:
        score = float(getattr(result, "score", 0.0) or 0.0)
        if score < threshold:
            continue
        attrs = getattr(result, "attributes", None) or {}
        text = _result_text(result)
        if not text:
            continue
        if len(text) > KNOWLEDGE_MATCH_MAX_CHARS:
            text = text[:KNOWLEDGE_MATCH_MAX_CHARS].rsplit(" ", 1)[0] + " …"
        matches.append({
            "article_id": attrs.get("article_id"),
            "article_name": attrs.get("article_name") or getattr(result, "filename", None),
            "scope_type": attrs.get("scope_type"),
            "scope_name": attrs.get("scope_name"),
            "score": round(score, 4),
            "text": text,
        })

    if not matches:
        return {
            "status": "not_found",
            "school": resolved_school.get("canonical_school") or school,
            "knowledge_group": resolved_school.get("parent_website") or None,
            "query": query,
            "message": "El índice no encontró fragmentos suficientemente relevantes para esta pregunta.",
        }

    return {
        "status": "ok",
        "school": resolved_school.get("canonical_school") or school,
        "knowledge_group": resolved_school.get("parent_website") or None,
        "query": query,
        "search_query": getattr(results, "search_query", query),
        "matches": matches,
        "instruction": (
            "Usa estos fragmentos como fuente factual. Combínalos con herramientas operativas si la pregunta "
            "también depende de un pedido o producto. No inventes lo que los fragmentos no dicen."
        ),
    }


def _format_prefetched_knowledge(prefetched: Optional[dict]) -> str:
    if not prefetched or prefetched.get("status") != "ok":
        return "No se recuperó conocimiento relevante automáticamente en este turno."

    lines = [
        f"Escuela canónica: {prefetched.get('school') or ''}",
        f"Grupo de conocimiento: {prefetched.get('knowledge_group') or 'ninguno'}",
        "Fragmentos semánticamente relevantes de Knowledge:",
    ]
    for idx, item in enumerate(prefetched.get("matches") or [], 1):
        lines.append(f"[{idx}] {item.get('article_name')}:\n{item.get('text')}")
    return "\n\n".join(lines)

def _available_fields(uid, models, model_name: str):
    """Lee los campos reales del modelo para tolerar diferencias entre bases/versiones de Odoo."""
    try:
        return _fields_get(uid, models, model_name, ["type"])
    except Exception:
        logger.warning("No se pudieron leer fields_get de %s", model_name, exc_info=True)
        return {}


def _absolute_website_url(base: Optional[str], path: Optional[str]) -> Optional[str]:
    path = (path or "").strip()
    if not path:
        return None

    if path.startswith("http://") or path.startswith("https://"):
        return path

    base = (base or ODOO_URL or "").strip()
    if not base:
        return None
    if not base.startswith("http://") and not base.startswith("https://"):
        base = "https://" + base

    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def buscar_producto_escuela(school: str, query: str):
    """
    Busca productos vendibles de una escuela y devuelve enlaces de eCommerce.

    Esta herramienta NO usa stock para decidir qué decir al cliente. El inventario puede
    estar desactualizado y SportHouse puede permitir comprar productos temporalmente sin
    existencia física. Su objetivo es encontrar el producto/página correcta de la escuela.
    """
    school = (school or "").strip()
    query = (query or "").strip()

    if not school:
        return {
            "status": "missing_school",
            "message": "Falta la escuela. Pregunta al cliente antes de buscar productos.",
        }

    uid, models = _odoo()

    resolved_school = _resolve_school(uid, models, school)
    if resolved_school.get("status") != "ok":
        return {
            "status": "school_unresolved",
            "school_input": school,
            "school_resolution": resolved_school,
            "message": "No pude identificar de forma segura la escuela. Pide una aclaración.",
        }
    canonical_school = resolved_school.get("canonical_school") or school

    # Resolver el sitio de la escuela para construir URLs correctas en multi-sitio.
    website_fields = _available_fields(uid, models, "website")
    website_read_fields = [f for f in ("id", "name", "domain") if f in website_fields or f in ("id", "name")]
    school_website_id = resolved_school.get("website_id")
    school_domain = resolved_school.get("domain") or ODOO_URL
    school_shop_url = _absolute_website_url(school_domain, "/shop")

    product_fields = _available_fields(uid, models, "product.template")
    read_fields = ["id", "name", "categ_id"]
    for optional in ("website_url", "website_id", "website_published", "is_published", "sale_ok", "active"):
        if optional in product_fields:
            read_fields.append(optional)

    # Frontera de catálogo para no mezclar colegios. Antes solo se buscaba la categoría con el nombre
    # de la escuela concreta, así que una escuela dentro de un grupo (p. ej. IMS en Maristas) no encontraba nada.
    group_name = resolved_school.get("parent_website")
    scope_clauses = [
        ["categ_id.complete_name", "ilike", name]
        for name in dict.fromkeys(n for n in (canonical_school, group_name) if n)
    ]
    if school_website_id and "website_id" in product_fields:
        scope_clauses.append(["website_id", "=", school_website_id])

    base_domain = _or_domain(scope_clauses)
    if "active" in product_fields:
        base_domain.append(["active", "=", True])
    if "sale_ok" in product_fields:
        base_domain.append(["sale_ok", "=", True])

    # Antes se leían los primeros 120 productos en orden alfabético y luego se buscaba ahí:
    # si la escuela tenía más, contestaba que no existía algo que sí existe. Ahora Odoo filtra por palabra.
    query_tokens_db = [
        t for t in re.findall(r"[a-z0-9]+", _norm(query))
        if len(t) >= 3 and t not in _PRODUCT_QUERY_STOPWORDS
    ]
    products = []
    if query_tokens_db:
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "search_read",
            [base_domain + _or_domain([["name", "ilike", t] for t in query_tokens_db[:6]])],
            {"fields": read_fields, "limit": 80, "order": "name asc"},
        ) or []
    if not products:
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.template", "search_read",
            [base_domain],
            {"fields": read_fields, "limit": 200, "order": "name asc"},
        ) or []

    if not products:
        return {
            "status": "not_found",
            "school": canonical_school,
            "query": query,
            "school_shop_url": school_shop_url,
            "message": "No se encontraron productos configurados para esa escuela.",
        }

    # Si algunos productos están asociados a un website concreto, obtenemos esos dominios.
    website_ids = {
        p.get("website_id")[0]
        for p in products
        if isinstance(p.get("website_id"), (list, tuple)) and p.get("website_id")
    }
    website_domain_by_id = {}
    if website_ids:
        all_websites = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "website", "search_read",
            [[['id', 'in', list(website_ids)]]],
            {"fields": website_read_fields, "limit": len(website_ids)},
        )
        website_domain_by_id = {
            w.get("id"): (w.get("domain") or ODOO_URL)
            for w in all_websites
        }

    query_norm = _norm(query)
    query_tokens = [
        t for t in re.findall(r"[a-z0-9]+", query_norm)
        if len(t) >= 2 and t not in _PRODUCT_QUERY_STOPWORDS
    ]
    school_norm = _norm(canonical_school)
    prefer_specific = bool(group_name) and _norm(group_name) != school_norm

    candidates = []
    for product in products:
        name = (product.get("name") or "").strip()
        name_norm = _norm(name)

        score = 0
        if query_norm and query_norm in name_norm:
            score += 100
        for token in query_tokens:
            if token in name_norm:
                score += 10

        category = product.get("categ_id")
        category_name = category[1] if isinstance(category, (list, tuple)) and len(category) > 1 else None
        # Dentro de un grupo, primero los productos de la escuela concreta.
        if prefer_specific and school_norm and school_norm in _norm(category_name):
            score += 5

        website_id_value = product.get("website_id")
        product_website_id = (
            website_id_value[0]
            if isinstance(website_id_value, (list, tuple)) and website_id_value
            else None
        )
        base_domain = website_domain_by_id.get(product_website_id) or school_domain

        published = None
        if "website_published" in product:
            published = bool(product.get("website_published"))
        elif "is_published" in product:
            published = bool(product.get("is_published"))

        product_url = _absolute_website_url(base_domain, product.get("website_url"))
        # Si Odoo confirma que no está publicado, no entregamos una URL de producto como comprable.
        if published is False:
            product_url = None

        candidates.append({
            "name": name,
            "category": category_name,
            "product_url": product_url,
            "published": published,
            "_score": score,
        })

    candidates.sort(key=lambda x: (-x["_score"], _norm(x["name"])))

    # Si hay coincidencias textuales, mandamos las mejores. Si no, damos una muestra más
    # amplia del catálogo para que la IA pueda resolver lenguaje natural/sinónimos.
    positive = [c for c in candidates if c["_score"] > 0]
    selected = (positive[:20] if positive else candidates[:45])
    for item in selected:
        item.pop("_score", None)

    return {
        "status": "ok",
        "school": canonical_school,
        "query": query,
        "school_website_id": school_website_id,
        "school_shop_url": school_shop_url,
        "products_considered": len(products),
        "candidates": selected,
        "inventory_policy": (
            "No afirmar hay/no hay stock con esta herramienta. "
            "Dirigir al cliente al producto o a la tienda de la escuela para revisar opciones y comprar."
        ),
    }


_PRODUCT_QUERY_STOPWORDS = {
    "de", "del", "la", "las", "el", "los", "un", "una", "unos", "unas", "para", "con", "sin", "por",
    "que", "tienen", "tiene", "venden", "vende", "hay", "busco", "quiero", "necesito", "talla", "tallas",
    "precio", "cuanto", "cuesta", "link", "liga", "comprar", "donde", "mi", "hijo", "hija",
}


def _or_domain(clauses: list) -> list:
    """Combina condiciones con OR en notación de dominio de Odoo."""
    clauses = [c for c in clauses if c]
    if not clauses:
        return []
    return ["|"] * (len(clauses) - 1) + list(clauses)


def escalar_asesor(reason: str):
    """Marca el caso para que ManyChat pueda enviarlo después a atención humana."""
    return {
        "status": "ok",
        "needs_human": True,
        "reason": (reason or "El caso requiere revisión humana.").strip(),
    }


TOOLS = [
    {
        "type": "function",
        "name": "consultar_pedido",
        "description": (
            "Consulta en Odoo un pedido real. Llámala en cuanto hayas resuelto con suficiente confianza "
            "un único número de pedido en formato S00000, aunque todavía no sepas la escuela: si el "
            "WhatsApp del cliente coincide con el teléfono del pedido, se verifica solo. Si devuelve "
            "missing_school, pregunta la escuela y vuelve a llamarla. "
            "No la llames si el cliente dio varios números, cree que le falta un dígito o existe ambigüedad: "
            "en esos casos conversa y pide aclaración primero."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "school": {
                    "type": "string",
                    "description": "Escuela confirmada por el cliente o ya conocida en el contexto. Cadena vacía si aún no se sabe.",
                },
                "order_number": {
                    "type": "string",
                    "description": "Número canónico único, por ejemplo S02038.",
                },
            },
            "required": ["school", "order_number"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "buscar_info_escuela",
        "description": (
            "Busca mediante RAG persistente en el índice semántico sincronizado desde Knowledge de Odoo para la escuela y su grupo. "
            "Úsala para cualquier información factual, política, instrucción, tiempo, lugar, link, pago, compra, "
            "cambio, entrega, recolección, guía o explicación que pueda vivir en Knowledge. No depende de palabras "
            "exactas ni de una categoría fija. Puede combinarse con consultar_pedido o buscar_producto_escuela en el mismo turno."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "school": {
                    "type": "string",
                    "description": "Escuela confirmada del cliente.",
                },
                "query": {
                    "type": "string",
                    "description": "Qué información necesitas encontrar para responder al cliente.",
                },
            },
            "required": ["school", "query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "buscar_producto_escuela",
        "description": (
            "Busca en el catálogo de Odoo los productos de una escuela y devuelve las páginas web correctas para comprar. "
            "Úsala cuando el cliente pregunte si venden/tienen un producto, una prenda, una talla, dónde comprarla o pida el link. "
            "La herramienta NO confirma stock físico: aunque Odoo marque cero puede permitirse la compra. "
            "Interpreta los candidatos y dirige al producto correcto o, si no hay coincidencia clara, a la tienda de la escuela."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "school": {
                    "type": "string",
                    "description": "Escuela confirmada del cliente.",
                },
                "query": {
                    "type": "string",
                    "description": "Descripción libre del producto/prenda que el cliente busca. Puede incluir talla, deporte, género u otros detalles.",
                },
            },
            "required": ["school", "query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "escalar_asesor",
        "description": (
            "Marca que un asesor humano debe continuar. Úsala cuando la información disponible no alcance, "
            "la herramienta indique que no se puede validar de forma segura, o el cliente pida hablar con una persona."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


FINAL_RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "sporthouse_chat_response",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": "Mensaje final natural y breve que se enviará al cliente por WhatsApp.",
            },
            "school": {
                "type": ["string", "null"],
                "description": "Escuela actualmente confirmada; conserva la anterior si no cambió.",
            },
            "order_number": {
                "type": ["string", "null"],
                "description": "Pedido actualmente confirmado, preferentemente en formato S00000. Null si no está confirmado.",
            },
            "product": {
                "type": ["string", "null"],
                "description": "Producto del que se está hablando, si quedó claro.",
            },
            "size": {
                "type": ["string", "null"],
                "description": "Talla mencionada/confirmada, si aplica.",
            },
            "intent": {
                "type": ["string", "null"],
                "description": "Descripción corta de la necesidad actual del cliente; no tiene que pertenecer a un catálogo fijo.",
            },
            "needs_human": {
                "type": "boolean",
                "description": "True solo cuando el caso realmente debe pasar a una persona.",
            },
            "escalation_reason": {
                "type": ["string", "null"],
                "description": "Razón interna breve si needs_human=true; no tiene que mostrarse al cliente.",
            },
        },
        "required": [
            "reply", "school", "order_number", "product", "size", "intent",
            "needs_human", "escalation_reason"
        ],
        "additionalProperties": False,
    },
}


INSTRUCTIONS = """
Eres el asistente conversacional de SportHouse para WhatsApp.

PRINCIPIO CENTRAL
Tú eres quien entiende la conversación. Las mamás pueden preguntar de cualquier manera: con errores,
abreviaciones, mensajes incompletos, varios temas a la vez, referencias como "ese", "el primero",
frases poco claras o números escritos de forma informal. No uses un árbol rígido de intenciones ni esperes
palabras exactas. Interpreta la conversación completa y decide qué necesitas preguntar, qué herramienta usar
y cómo responder.

ESCUELAS
SportHouse atiende muchas escuelas y existen productos iguales o parecidos entre ellas. Para cualquier dato
que dependa de catálogo, producto, inventario, talla, precio, guía, política, entrega o FAQ debes
conocer primero la escuela. Excepción: para consultar un pedido con número claro llama primero consultar_pedido
(la escuela puede ir vacía); solo pregunta la escuela si la herramienta devuelve missing_school. Nunca adivines la escuela por una prenda. Si no está clara, pregúntala de forma
natural. Si ya está confirmada en la conversación o en CONTEXTO EXTERNO, no la vuelvas a pedir. Si el cliente
cambia de escuela explícitamente, actualiza el contexto.

IMPORTANTE: escuela y sitio/grupo NO siempre son lo mismo. Una escuela concreta puede vivir dentro de un
website de grupo (por ejemplo, una escuela específica dentro de Maristas). Las herramientas pueden devolver
`school` como la escuela concreta y `website_group`/`parent_website` como el grupo. Cuando exista una escuela
específica en el pedido, esa escuela tiene prioridad para validar al cliente; el nombre del grupo por sí solo
puede ser insuficiente. Las herramientas resuelven abreviaciones y siglas contra nombres reales de escuela
configurados en Odoo. Cuando una herramienta devuelva un nombre canónico de escuela, consérvalo en `school`.
Si devuelve `specific_school_required`, pregunta la escuela concreta.

ESCUELA MAL ESCRITA (muy común: las mamás escriben rápido, con errores, sin acentos o abreviado)
- En CONTEXTO EXTERNO recibes la lista de ESCUELAS CONFIGURADAS. Si lo que escribió se parece claramente a una
  sola de ellas ("monte berde", "maristaz", "edrom"), NO le digas que no existe: confirma UNA sola vez de forma
  breve y natural, por ejemplo: "¿Te refieres a *Monteverde*? 😊". Puedes, en el mismo mensaje, adelantar qué
  vas a revisar.
- Si una herramienta devuelve `confirm_school` (o `school_unresolved` con `school_resolution.status=confirm_school`),
  pregunta exactamente por `suggestion`, una sola vez.
- Cuando la mamá confirme con cualquier forma afirmativa ("sí", "esa", "ajá", "correcto", 👍, o repite el nombre
  aunque mal escrito), toma esa escuela como confirmada: llama las herramientas con el nombre EXACTO de la lista,
  guárdalo en `school` y continúa con lo que había preguntado originalmente, sin que lo tenga que repetir.
- Nunca vuelvas a preguntar por la escuela una vez confirmada en la conversación o en CONTEXTO EXTERNO.
- Si dice que no, o si hay varias parecidas (`ambiguous`), muestra las opciones (máximo 5) para que elija.
- Si no se parece a ninguna, pregunta de nuevo de forma amable; puedes listar las escuelas disponibles.

PEDIDOS
El formato canónico de SportHouse es S + 5 dígitos, por ejemplo S02038. El cliente NO tiene que escribirlo
perfectamente. Tú puedes comprender variantes de escritura y convertirlas al formato canónico cuando sea
inequívoco. Ejemplos de transformación inequívoca pueden ser mayúsculas/minúsculas, espacios, guiones,
una S omitida o un cero inicial omitido cuando el resto identifica claramente el mismo número.

La regla no es memorizar ejemplos: usa criterio conversacional.
- Si hay un único número inequívoco, llama consultar_pedido con S00000 (con la escuela si ya la conoces, o vacía).
- Si devuelve missing_school, el WhatsApp no coincide con el del pedido: pregunta de qué escuela es, sin revelar nada.
- Si hay varios números posibles, no elijas por el cliente: pregunta cuál quiere revisar.
- Si el cliente dice o sugiere que falta/sobra un dígito, o no puedes reconstruir un único S00000 con confianza,
  no inventes: pide que lo confirme.
- Si un número canónico no existe, pide verificarlo. No cambies dígitos para encontrar otro pedido.
- Si consultar_pedido devuelve school_mismatch, no reveles el estado ni otros datos del pedido.
- La verificación del teléfono la hace la herramienta; nunca pidas ni menciones el teléfono del pedido.
- Para responder estatus usa `resumen` y `paquetes` (cada paquete trae su sporthouse_status). internal_delivery_state
  es un dato interno y no debe sustituir ni reinterpretar el Estado SportHouse ante el cliente.
- Cuando consultar_pedido devuelva status ok, responde con este formato, breve y claro para WhatsApp:
  📦 *Pedido S00000* · Escuela
  *Estado:* <resumen>
  (línea en blanco)
  Por cada paquete: "✅ Paquete N · Entregado <lugar_entrega> el <fecha_entrega>" si entregado,
  o "🚚 Paquete N · <sporthouse_status> · llega aprox. <fecha_estimada>" si no; debajo, en una línea, sus productos
  separados por coma (si son muchos, los 3 primeros y "y N más"). Si solo hay un paquete, no digas "Paquete 1".
  Omite lugar o fecha si vienen vacíos; nunca inventes fechas.
  (línea en blanco)
  "Ver detalle de mi pedido: <detail_url>" (copia la liga exacta, completa, sin formato markdown).
  Cierra con una sola frase corta ofreciendo ayuda. No agregues datos que no vengan en la herramienta.

FLUJO UNIVERSAL DE CONOCIMIENTO
Este criterio aplica a TODAS las preguntas de SportHouse, no a una lista cerrada de temas.
Una vez que la escuela esté clara, antes de responder una pregunta factual revisa el CONOCIMIENTO RECUPERADO
AUTOMÁTICAMENTE incluido en el contexto. Ese contenido fue seleccionado semánticamente desde Knowledge de Odoo
para la escuela concreta y, cuando corresponda, su grupo.

Si el conocimiento precargado no basta y la pregunta puede depender de información de SportHouse, llama
buscar_info_escuela con una consulta descriptiva de lo que necesitas saber. No esperes palabras exactas ni
clasifiques la pregunta en un catálogo rígido. La búsqueda usa un índice vectorial persistente, filtrado por escuela/grupo y con reescritura automática de consultas.

Puedes y debes combinar Knowledge con herramientas operativas cuando sea necesario. Ejemplos conceptuales:
- un estatus de pedido puede requerir consultar_pedido + Knowledge para explicar qué sigue, dónde se entrega o tiempos;
- una pregunta de producto puede requerir buscar_producto_escuela + Knowledge para explicar cómo comprar;
- una pregunta de políticas puede resolverse solo con Knowledge.

El resultado de una herramienta NO significa automáticamente que la consulta completa esté resuelta. Antes de
responder, verifica si aún falta información relevante. Antes de escalar por falta de información, si la escuela
está clara y existe una posibilidad razonable de que la respuesta viva en Knowledge, consulta Knowledge primero.
Si una herramienta devuelve `tool_error`, NO concluyas que la información no existe y NO escales automáticamente: utiliza el conocimiento ya recuperado en el contexto; si falta un dato esencial, pide una aclaración breve.
Responde únicamente con información respaldada por las herramientas/contexto recuperado.

PRODUCTOS, TALLAS Y COMPRA
Para preguntas como “¿tienen hoodie?”, “¿venden pants?”, “¿hay talla M?”, “¿dónde compro esta prenda?” o
“pásame el link”, usa buscar_producto_escuela cuando la escuela esté clara. Esa herramienta encuentra el
catálogo y las páginas web correctas de la escuela.

MUY IMPORTANTE: el inventario físico de Odoo puede no estar totalmente actualizado y SportHouse puede permitir
comprar productos aunque temporalmente no haya existencia física. Por eso:
- Nunca conviertas stock interno en una afirmación de “sí hay” o “no hay”.
- No prometas existencia física ni cantidad disponible.
- Si la herramienta encuentra el producto, dirige al cliente a su página para revisar opciones y comprar.
- Si preguntan por una talla, puedes decir que revise/seleccione las opciones disponibles en la página; no afirmes
  disponibilidad física de esa talla salvo que en el futuro exista una fuente explícita autorizada para ello.
- Si hay varios productos plausibles, conversa para identificar cuál busca en vez de elegir al azar.
- Si no hay coincidencia clara pero existe school_shop_url, puedes dirigir a la tienda de la escuela.
- Nunca mandes un link de producto de otra escuela.

VARIOS TEMAS EN EL MISMO MENSAJE
No fuerces una sola intención. Si una mamá pregunta dos o más cosas, resuelve todas las que puedas. Puedes
usar más de una herramienta en el mismo turno. Si necesitas una aclaración que bloquea solo una parte,
puedes responder la otra parte y preguntar lo que falta.

CONTEXTO Y MEMORIA
Recibirás CONTEXTO EXTERNO con escuela/pedido/producto/talla que ManyChat haya guardado. También puedes
recibir el historial completo mediante una conversación persistente. Conserva en tu salida los datos ya confirmados,
salvo que el cliente los corrija o cambie explícitamente. No guardes como definitivo un dato que tú mismo
consideras ambiguo.

ESCALAMIENTO
No digas "ya te pasé con un asesor" a menos que realmente hayas llamado escalar_asesor. Si lo llamas,
puedes decir que un asesor continuará o revisará el caso. No escales solo porque el cliente escribió raro:
primero conversa y aclara cuando sea razonable. Tampoco escales por falta de información factual sin haber
revisado el conocimiento RAG precargado y, cuando corresponda, haber llamado buscar_info_escuela. Si Knowledge devuelve
fragmentos relevantes, aprovéchalos antes de escalar; un estado de pedido por sí solo no responde preguntas sobre qué sigue.

ESTILO
- Español por defecto.
- Breve, amable y natural, como una buena atención por WhatsApp.
- Emojis moderados.
- Para negritas de WhatsApp usa *texto*, no **texto**.
- No menciones OpenAI, ChatGPT, Odoo, APIs, prompts, funciones ni herramientas.
- Nunca inventes precios, inventario, estatus, fechas, políticas, links ni datos de otra escuela.

SALIDA
Tu respuesta final debe seguir el esquema estructurado recibido. `reply` es exactamente lo que verá el cliente.
Los demás campos son contexto operativo para conservar la conversación.
""".strip()


def _external_context(data: ChatRequest, prefetched_knowledge: Optional[dict] = None) -> str:
    school_list = ""
    try:
        uid, models = _odoo()
        school_list = ", ".join(_school_names(uid, models)[:60])
    except Exception:
        logger.warning("No se pudo armar la lista de escuelas para el contexto", exc_info=True)
    return f"""
CONTEXTO EXTERNO ACTUAL (puede contener valores vacíos):
- ESCUELAS CONFIGURADAS: {school_list or 'no disponible en este momento'}
- escuela confirmada: {data.school or 'NO CONOCIDA'}
- pedido confirmado: {data.order_number or 'NO CONOCIDO'}
- producto: {data.product or 'NO CONOCIDO'}
- talla: {data.size or 'NO CONOCIDA'}
- necesidad/intención previa: {data.intent or 'NO CONOCIDA'}
- nombre del cliente: {data.first_name or 'NO DISPONIBLE'}

CONOCIMIENTO RECUPERADO AUTOMÁTICAMENTE PARA ESTE TURNO:
{_format_prefetched_knowledge(prefetched_knowledge)}

MENSAJE NUEVO DEL CLIENTE:
{(data.message or '').strip()}
""".strip()


def _fallback_context(data: ChatRequest):
    return {
        "school": data.school or None,
        "order_number": data.order_number or None,
        "product": data.product or None,
        "size": data.size or None,
        "intent": data.intent or None,
    }


class _ModelOutputError(RuntimeError):
    """El modelo no devolvió un JSON final válido (p. ej. se quedó sin tokens)."""


def _parse_final_response(response, data: ChatRequest, needs_human_from_tool=False, escalation_reason_from_tool=None):
    status = getattr(response, "status", None)
    raw = (getattr(response, "output_text", None) or "").strip()
    if status == "incomplete" or not raw:
        details = getattr(response, "incomplete_details", None)
        raise _ModelOutputError(f"Respuesta incompleta del modelo: status={status} details={details}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _ModelOutputError(f"JSON inválido del modelo: {raw[:200]}") from exc
    if not (parsed.get("reply") or "").strip():
        raise _ModelOutputError("El modelo devolvió reply vacío")

    # El modelo administra el contexto. Solo usamos el contexto previo como fallback
    # si por alguna razón un campo viene vacío sin que se haya confirmado un reemplazo.
    previous = _fallback_context(data)
    for key in ("school", "order_number", "product", "size", "intent"):
        if parsed.get(key) in (None, "") and previous.get(key):
            parsed[key] = previous[key]

    if needs_human_from_tool:
        parsed["needs_human"] = True
        if not parsed.get("escalation_reason"):
            parsed["escalation_reason"] = escalation_reason_from_tool

    parsed["response_id"] = response.id
    return parsed


# Mensajes que no necesitan buscar en Knowledge antes de contestar (ahorra tiempo y evita
# meterle a la IA fragmentos irrelevantes como si fueran la respuesta).
_TRIVIAL_MESSAGE_RE = re.compile(
    r"^(hola|buen[oa]s?( dias| tardes| noches)?|gracias|muchas gracias|ok|okay|va|sale|perfecto|"
    r"si|no|listo|claro|de acuerdo|excelente|genial|mil gracias|bye|adios)[\s!.,?¡¿]*$"
)


def _should_prefetch_knowledge(message: Optional[str]) -> bool:
    norm = _norm(message)
    if not norm:
        return False
    if _TRIVIAL_MESSAGE_RE.match(norm):
        return False
    # Si trae número de pedido, la IA va a consultar Odoo; buscar en Knowledge solo agrega segundos.
    if re.search(r"\bs\s*-?\s*\d{3,7}\b", norm):
        return False
    words = re.findall(r"[a-z]{2,}", norm)
    # Solo un número de pedido o un par de palabras: no hay pregunta que buscar.
    if len(words) < 3:
        return False
    return True


_MODEL_ACCEPTS_REASONING = {"value": None}


def _model_maybe_reasoning() -> bool:
    name = (OPENAI_MODEL or "").lower()
    return bool(OPENAI_REASONING_EFFORT) and (name.startswith("gpt-5") or re.match(r"^o\d", name) is not None)


def _is_conversation_error(exc: Exception) -> bool:
    if not isinstance(exc, (openai.BadRequestError, openai.NotFoundError)):
        return False
    return "conversation" in str(exc).lower()


def _openai_failure_reason(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, openai.RateLimitError) and (
        "insufficient_quota" in text or "credit_balance_exhausted" in text
    ):
        return "openai_sin_credito"
    if isinstance(exc, openai.RateLimitError):
        return "openai_rate_limit"
    if isinstance(exc, openai.AuthenticationError):
        return "openai_api_key_invalida"
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return "openai_timeout"
    if isinstance(exc, _ModelOutputError):
        return "respuesta_incompleta_del_modelo"
    return "backend_error"


# Un solo turno a la vez por contacto: dos mensajes seguidos de la misma mamá no deben
# correr en paralelo sobre la misma conversación (se cruzaban respuestas).
_CONTACT_LOCKS: dict = {}
_CONTACT_LOCKS_GUARD = threading.Lock()


def _contact_lock(data: ChatRequest) -> threading.Lock:
    key = data.contact_id or data.phone or data.conversation_id or "_anon"
    with _CONTACT_LOCKS_GUARD:
        lock = _CONTACT_LOCKS.get(key)
        if lock is None:
            if len(_CONTACT_LOCKS) > 5000:
                _CONTACT_LOCKS.clear()
            lock = threading.Lock()
            _CONTACT_LOCKS[key] = lock
        return lock


def _error_result(data: ChatRequest, reason: str, conversation_id, tools_used, knowledge_sources):
    return {
        "reply": "Tuve un problema al revisar la información. Un asesor de SportHouse te ayudará en breve. 🙏",
        **_fallback_context(data),
        "needs_human": True,
        "escalation_reason": reason,
        "conversation_id": conversation_id,
        "response_id": data.previous_response_id,
        "tools_used": tools_used,
        "knowledge_sources": list(dict.fromkeys(knowledge_sources)),
    }


def process_chat(data: ChatRequest) -> dict:
    """Lógica completa de un turno. La usan /chat (síncrono) y /chat_async (en segundo plano)."""
    started = time.time()
    if not data.message:
        return {
            "reply": "¿En qué puedo ayudarte? 😊",
            **_fallback_context(data),
            "needs_human": False,
            "escalation_reason": None,
            "conversation_id": data.conversation_id,
            "response_id": data.previous_response_id,
            "tools_used": [],
            "knowledge_sources": [],
        }

    if not OPENAI_API_KEY:
        return _error_result(data, "openai_not_configured", data.conversation_id, [], [])

    client = _openai_client()
    tools_used = []
    knowledge_sources = []
    needs_human_from_tool = False
    escalation_reason_from_tool = None
    conversation_id = data.conversation_id

    try:
        # --------------------------------------------------------------
        # 1) Conversación persistente (memoria de la IA)
        # --------------------------------------------------------------
        conversation_was_created = False

        def _new_conversation(recovered: bool = False):
            metadata = {"source": "manychat-sporthouse"}
            if recovered:
                metadata["recovered"] = "true"
            if data.contact_id:
                metadata["contact_id"] = str(data.contact_id)[:512]
            return client.conversations.create(metadata=metadata).id

        if not conversation_id:
            conversation_id = _new_conversation()
            conversation_was_created = True

        # --------------------------------------------------------------
        # 2) Precarga de Knowledge solo si hay una pregunta real y la escuela se conoce.
        # --------------------------------------------------------------
        prefetched_knowledge = None
        if data.school and _should_prefetch_knowledge(data.message):
            try:
                prefetched_knowledge = buscar_info_escuela(
                    school=data.school,
                    query=data.message,
                    score_threshold=KNOWLEDGE_PREFETCH_THRESHOLD,
                    max_results=KNOWLEDGE_PREFETCH_MAX_RESULTS,
                    # Sin reescribir la pregunta con otro modelo: ahorra ~1-2 s por mensaje.
                    rewrite_query=False,
                )
                if prefetched_knowledge.get("status") == "ok":
                    knowledge_sources.extend([
                        item.get("article_name")
                        for item in prefetched_knowledge.get("matches") or []
                        if item.get("article_name")
                    ])
            except Exception:
                logger.warning("No se pudo precargar Knowledge; el agente podrá buscarlo como tool", exc_info=True)
                prefetched_knowledge = None

        def _create_response(input_items):
            kwargs = dict(
                model=OPENAI_MODEL,
                instructions=INSTRUCTIONS,
                conversation=conversation_id,
                input=input_items,
                tools=TOOLS,
                tool_choice="auto",
                text={"format": FINAL_RESPONSE_FORMAT},
                max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
                # Si la conversación crece demasiado, se recortan los turnos viejos en vez de fallar.
                truncation="auto",
            )
            use_reasoning = _MODEL_ACCEPTS_REASONING["value"] is not False and _model_maybe_reasoning()
            if use_reasoning:
                kwargs["reasoning"] = {"effort": OPENAI_REASONING_EFFORT}
            try:
                response = client.responses.create(**kwargs)
            except openai.BadRequestError as exc:
                if use_reasoning and "reasoning" in str(exc).lower():
                    _MODEL_ACCEPTS_REASONING["value"] = False
                    kwargs.pop("reasoning", None)
                    response = client.responses.create(**kwargs)
                else:
                    raise
            if use_reasoning:
                _MODEL_ACCEPTS_REASONING["value"] = True
            return response

        first_input = [{"role": "user", "content": _external_context(data, prefetched_knowledge)}]
        try:
            response = _create_response(first_input)
        except Exception as exc:
            # Solo se crea una conversación nueva si el ID guardado es inválido/viejo.
            # (Antes también lo hacía ante errores de crédito, duplicando el tiempo de espera.)
            if conversation_was_created or not _is_conversation_error(exc):
                raise
            logger.warning("Conversation inválida/no disponible (%s); creando una nueva", conversation_id)
            conversation_id = _new_conversation(recovered=True)
            response = _create_response(first_input)

        # El modelo puede encadenar varias herramientas en el mismo turno.
        for _ in range(6):
            function_calls = [
                item for item in response.output
                if getattr(item, "type", None) == "function_call"
            ]

            if not function_calls:
                result = _parse_final_response(
                    response,
                    data,
                    needs_human_from_tool=needs_human_from_tool,
                    escalation_reason_from_tool=escalation_reason_from_tool,
                )
                result["tools_used"] = tools_used
                result["knowledge_sources"] = list(dict.fromkeys(knowledge_sources))
                result["conversation_id"] = conversation_id
                # Conservamos response_id solo como diagnóstico/compatibilidad. No se usa como memoria principal.
                result["response_id"] = response.id
                logger.info(
                    "chat ok en %.1fs tools=%s contact=%s",
                    time.time() - started, tools_used, data.contact_id,
                )
                return result

            tool_outputs = []
            for call in function_calls:
                try:
                    args = json.loads(call.arguments or "{}")
                except Exception:
                    args = {}

                tools_used.append(call.name)

                try:
                    if call.name == "consultar_pedido":
                        caller_phone = data.phone if (data.phone and not _is_placeholder(data.phone)) else None
                        if not caller_phone:
                            caller_phone = _manychat_phone(data.contact_id)
                        tool_result = consultar_pedido(
                            school=args.get("school"),
                            order_number=args.get("order_number"),
                            phone=caller_phone,
                        )

                    elif call.name == "buscar_info_escuela":
                        tool_result = buscar_info_escuela(
                            school=args.get("school"),
                            query=args.get("query"),
                        )
                        if tool_result.get("status") == "ok":
                            knowledge_sources.extend([
                                item.get("article_name")
                                for item in tool_result.get("matches") or []
                                if item.get("article_name")
                            ])

                    elif call.name == "buscar_producto_escuela":
                        tool_result = buscar_producto_escuela(
                            school=args.get("school"),
                            query=args.get("query"),
                        )

                    elif call.name == "escalar_asesor":
                        tool_result = escalar_asesor(args.get("reason"))
                        needs_human_from_tool = True
                        escalation_reason_from_tool = tool_result.get("reason")

                    else:
                        tool_result = {
                            "status": "error",
                            "message": "Herramienta no disponible.",
                        }
                except Exception as tool_exc:
                    logger.exception("Error ejecutando herramienta %s", call.name)
                    tool_result = {
                        "status": "tool_error",
                        "tool": call.name,
                        "message": "La herramienta tuvo un error técnico. No afirmes que la información no existe. Continúa con el contexto disponible y, si aún falta un dato esencial, pide una aclaración breve en lugar de inventar.",
                        "debug_type": type(tool_exc).__name__,
                    }

                tool_outputs.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(tool_result, ensure_ascii=False),
                })

            # Como el response y los function calls ya forman parte de la misma Conversation,
            # solo enviamos las salidas de herramientas y continuamos sobre conversation_id.
            response = _create_response(tool_outputs)

        return {
            "reply": "Necesito que un asesor de SportHouse continúe contigo para revisar este caso.",
            **_fallback_context(data),
            "needs_human": True,
            "escalation_reason": "tool_loop_limit",
            "conversation_id": conversation_id,
            "response_id": response.id,
            "tools_used": tools_used,
            "knowledge_sources": list(dict.fromkeys(knowledge_sources)),
        }

    except Exception as exc:
        reason = _openai_failure_reason(exc)
        if reason == "openai_sin_credito":
            logger.error("OpenAI SIN CRÉDITO: recarga en platform.openai.com/settings/organization/billing")
        else:
            logger.exception("Error en chat (%s)", reason)
        return _error_result(data, reason, conversation_id, tools_used, knowledge_sources)


@router.get("/knowledge/search")
def knowledge_search_debug(
    school: str,
    query: str,
    x_knowledge_sync_key: Optional[str] = Header(default=None, alias="X-Knowledge-Sync-Key"),
):
    """Diagnóstico genérico del RAG. No lo usa ManyChat; sirve para verificar qué Knowledge recupera una consulta."""
    if not KNOWLEDGE_SYNC_KEY or x_knowledge_sync_key != KNOWLEDGE_SYNC_KEY:
        return {"status": "unauthorized", "message": "Sync key inválida."}
    try:
        result = buscar_info_escuela(school=school, query=query)
        # Limitar texto para que Swagger sea legible, conservando título/score/fuente.
        if result.get("status") == "ok":
            compact = dict(result)
            compact["matches"] = [
                {**item, "text": (item.get("text") or "")[:1800]}
                for item in (result.get("matches") or [])[:10]
            ]
            return compact
        return result
    except Exception as exc:
        logger.exception("Error en GET /knowledge/search")
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }


@router.get("/knowledge/status")
def knowledge_status():
    if not OPENAI_API_KEY:
        return {"status": "error", "message": "OPENAI_API_KEY no configurada"}
    client = _openai_client()
    vector_store_id = _get_vector_store_id(client, create_if_missing=False)
    if not vector_store_id:
        return {
            "status": "not_synced",
            "vector_store_name": OPENAI_KNOWLEDGE_VECTOR_STORE_NAME,
            "sync_key_configured": bool(KNOWLEDGE_SYNC_KEY),
        }
    try:
        store = client.vector_stores.retrieve(vector_store_id=vector_store_id)
        counts = getattr(store, "file_counts", None)
        if hasattr(counts, "model_dump"):
            counts = counts.model_dump()
        return {
            "status": "ok",
            "vector_store_id": vector_store_id,
            "vector_store_name": getattr(store, "name", OPENAI_KNOWLEDGE_VECTOR_STORE_NAME),
            "file_counts": counts,
            "sync_key_configured": bool(KNOWLEDGE_SYNC_KEY),
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@router.post("/knowledge/sync")
def knowledge_sync(
    background_tasks: BackgroundTasks,
    completo: bool = False,
    x_knowledge_sync_key: Optional[str] = Header(default=None, alias="X-Knowledge-Sync-Key"),
):
    """Por defecto sube solo cambios; con ?completo=true reconstruye todo el índice."""
    if not KNOWLEDGE_SYNC_KEY:
        return {
            "status": "error",
            "message": "Configura KNOWLEDGE_SYNC_KEY en Railway antes de habilitar la sincronización.",
        }
    if x_knowledge_sync_key != KNOWLEDGE_SYNC_KEY:
        return {"status": "unauthorized", "message": "Sync key inválida."}

    if _KNOWLEDGE_SYNC_STATE.get("status") == "running":
        return {
            "status": "already_running",
            "message": _KNOWLEDGE_SYNC_STATE.get("message"),
            "started_at": _KNOWLEDGE_SYNC_STATE.get("started_at"),
        }

    background_tasks.add_task(_run_knowledge_sync_job, completo)
    return {
        "status": "started",
        "message": "Sincronización iniciada en segundo plano. Consulta GET /knowledge/sync/status para ver el progreso.",
    }


@router.get("/knowledge/sync/status")
def knowledge_sync_status():
    state = dict(_KNOWLEDGE_SYNC_STATE)
    result = state.get("result")
    # Evitamos devolver cientos de artículos en cada consulta de progreso.
    if isinstance(result, dict):
        state["result"] = {
            "status": result.get("status"),
            "vector_store_id": result.get("vector_store_id"),
            "vector_store_name": result.get("vector_store_name"),
            "removed_previous_files": result.get("removed_previous_files"),
            "odoo_articles_found": result.get("odoo_articles_found"),
            "indexed_count": result.get("indexed_count"),
            "skipped_count": result.get("skipped_count"),
            "skipped": result.get("skipped", [])[:20],
            "mode": result.get("mode", "completo"),
            "new": (result.get("new") or [])[:30],
            "updated": (result.get("updated") or [])[:30],
            "removed_files": result.get("removed_files"),
        }
    return state


@router.post("/chat")
def chat(data: ChatRequest):
    """
    Entrada síncrona del agente SportHouse (ManyChat espera la respuesta, máx. 10 s).

    Es `def` y no `async def` a propósito: todo el trabajo (OpenAI, Odoo) es bloqueante y en
    `async def` congelaba el servidor completo, así que dos mamás escribiendo a la vez se formaban.
    """
    with _contact_lock(data):
        return process_chat(data)


def _manychat_post(path: str, payload: dict):
    response = requests.post(
        f"{MANYCHAT_API}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"},
        timeout=15,
    )
    if not response.ok or (response.headers.get("content-type", "").startswith("application/json")
                           and response.json().get("status") == "error"):
        raise RuntimeError(f"ManyChat {path} {response.status_code}: {response.text[:300]}")
    return response


def _deliver_to_manychat(data: ChatRequest, result: dict):
    """Primero manda la respuesta por WhatsApp (lo que la mamá espera) y luego guarda el contexto."""
    subscriber_id = int(data.contact_id)
    started = time.time()
    _manychat_post("/fb/sending/sendContent", {
        "subscriber_id": subscriber_id,
        "data": {
            "version": "v2",
            "content": {
                "type": "whatsapp",
                "messages": [{"type": "text", "text": result.get("reply")}],
            },
        },
    })
    logger.info("Respuesta enviada por ManyChat a %s en %.1fs", subscriber_id, time.time() - started)

    values = {
        MC_FIELDS["conversation_id"]: result.get("conversation_id"),
        MC_FIELDS["school"]: result.get("school"),
        MC_FIELDS["order_number"]: result.get("order_number"),
        MC_FIELDS["intent"]: result.get("intent"),
        MC_FIELDS["needs_human"]: bool(result.get("needs_human")),
        MC_FIELDS["response_id"]: result.get("response_id"),
        MC_FIELDS["reply"]: result.get("reply"),
    }
    for field_name, value in values.items():
        if value in (None, ""):
            continue
        try:
            _manychat_post("/fb/subscriber/setCustomFieldByName", {
                "subscriber_id": subscriber_id,
                "field_name": field_name,
                "field_value": value,
            })
        except Exception:
            logger.warning("No se pudo guardar el campo %s en ManyChat", field_name, exc_info=True)

    if result.get("needs_human") and MC_HUMAN_TAG:
        try:
            _manychat_post("/fb/subscriber/addTagByName", {
                "subscriber_id": subscriber_id,
                "tag_name": MC_HUMAN_TAG,
            })
        except Exception:
            logger.warning("No se pudo poner la etiqueta %s", MC_HUMAN_TAG, exc_info=True)


def _run_chat_async(data: ChatRequest):
    try:
        with _contact_lock(data):
            result = process_chat(data)
            _deliver_to_manychat(data, result)
    except Exception:
        logger.exception("Error en chat_async para contacto %s", data.contact_id)


@router.post("/chat_async")
def chat_async(data: ChatRequest):
    """
    Modo sin límite de 10 s: responde a ManyChat al instante y, cuando la IA termina,
    manda el mensaje directo por la API de ManyChat. Requiere contact_id y MANYCHAT_API_KEY.
    """
    if not data.contact_id or not str(data.contact_id).isdigit():
        return {"status": "error", "message": "Falta contact_id válido."}
    if not MANYCHAT_API_KEY:
        return {"status": "error", "message": "Falta MANYCHAT_API_KEY."}
    threading.Thread(target=_run_chat_async, args=(data,), daemon=True).start()
    return {"status": "accepted"}


def _knowledge_auto_sync_loop():
    """Revisa Knowledge cada pocos minutos y sube solo lo que cambió (artículos o carpetas)."""
    time.sleep(90)  # deja arrancar el servicio antes de la primera revisión
    while True:
        if OPENAI_API_KEY and _KNOWLEDGE_SYNC_STATE.get("status") != "running":
            _run_knowledge_sync_job(full=False)
            state = _KNOWLEDGE_SYNC_STATE
            result = state.get("result") or {}
            if state.get("status") == "failed":
                logger.warning("Sync automático de Knowledge falló: %s", state.get("error"))
            elif result.get("new") or result.get("updated") or result.get("removed_files"):
                logger.info(
                    "Knowledge actualizado: nuevos=%s editados=%s archivos_quitados=%s",
                    result.get("new"), result.get("updated"), result.get("removed_files"),
                )
        time.sleep(max(1.0, KNOWLEDGE_SYNC_INTERVAL_MINUTES) * 60)


if KNOWLEDGE_SYNC_INTERVAL_MINUTES > 0:
    threading.Thread(target=_knowledge_auto_sync_loop, daemon=True, name="knowledge-auto-sync").start()
