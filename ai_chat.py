from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import html
import json
import logging
import os
import re
import unicodedata
import hashlib
import math
import time
import xmlrpc.client
from urllib.parse import urljoin

from openai import OpenAI

router = APIRouter()
logger = logging.getLogger("sporthouse-ai")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")


class ChatRequest(BaseModel):
    message: str
    contact_id: Optional[str] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None

    # Contexto durable que ManyChat puede guardar como custom fields.
    school: Optional[str] = None
    order_number: Optional[str] = None
    product: Optional[str] = None
    size: Optional[str] = None
    intent: Optional[str] = None

    # Permite que OpenAI conserve el hilo conversacional completo entre turnos.
    # ManyChat podrá guardar este valor y reenviarlo en el siguiente mensaje.
    previous_response_id: Optional[str] = None


def _norm(value: Optional[str]) -> str:
    value = (value or "").strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", value)
        if unicodedata.category(c) != "Mn"
    )




_SCHOOL_STOPWORDS = {
    "de", "del", "la", "las", "el", "los", "y", "e", "campus",
}

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
        fields = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "fields_get", [],
            {"attributes": ["type", "string", "selection"]},
        ) or {}
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


def _resolve_school(uid, models, school: str):
    """
    Resuelve lo que escribió el cliente contra escuelas reales y grupos/sitios de Odoo.
    Una escuela puede vivir dentro de un website de grupo; canonical_school y parent_website son distintos.
    """
    school = (school or "").strip()
    if not school:
        return {"status": "missing_school"}

    registry = _school_registry(uid, models)
    scored = []
    for item in registry:
        score = _school_match_score(school, item.get("canonical_school"))
        if score > 0:
            scored.append((score, item))

    if not scored:
        return {
            "status": "not_found",
            "input": school,
            "message": "No pude relacionar ese nombre con una escuela configurada en Odoo.",
        }

    scored.sort(key=lambda item: (-item[0], _norm(item[1].get("canonical_school"))))
    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0

    if best_score < 58 or (second_score >= best_score - 4 and best_score < 90):
        return {
            "status": "ambiguous",
            "input": school,
            "candidates": [
                {
                    "name": item.get("canonical_school"),
                    "group": item.get("parent_website"),
                    "score": round(score, 1),
                }
                for score, item in scored[:5]
            ],
            "message": "El nombre puede corresponder a más de una escuela. Pide al cliente que aclare cuál.",
        }

    return {
        "status": "ok",
        "input": school,
        "canonical_school": best.get("canonical_school"),
        "website_id": best.get("website_id"),
        "domain": best.get("domain"),
        "parent_website": best.get("parent_website"),
        "source": best.get("source"),
        "source_field": best.get("source_field"),
        "score": round(best_score, 1),
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

    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError("No se pudo autenticar con Odoo")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def consultar_pedido(school: str, order_number: str):
    """
    Consulta un pedido real y valida la escuela concreta del pedido.

    Importante: una escuela puede vivir dentro de un website/grupo (ej. Maristas).
    Si el pedido tiene un campo Studio de escuela específica, ese dato tiene prioridad
    sobre website_id para validar la escuela del cliente.
    """
    school = (school or "").strip()
    order_number = re.sub(r"[\s#\-]", "", (order_number or "").upper())

    if not school:
        return {
            "status": "missing_school",
            "message": "Falta la escuela. Pregunta al cliente antes de consultar el pedido.",
        }

    if not re.fullmatch(r"S\d{5}", order_number):
        return {
            "status": "invalid_format",
            "received": order_number,
            "expected_format": "S00000",
            "message": "El número todavía no está en formato canónico. No adivines si hay ambigüedad.",
        }

    uid, models = _odoo()

    # Descubrimos campos Studio que identifican escuela dentro de un grupo/sitio.
    school_field_meta = _sale_order_school_fields(uid, models)
    order_fields = ["id", "name", "website_id"] + list(school_field_meta.keys())

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

    pickings = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "stock.picking", "search_read",
        [[['origin', '=', order_number]]],
        {
            "fields": ["state", "x_studio_estado_sporthouse"],
            "limit": 1,
            "order": "id desc",
        },
    )

    picking = pickings[0] if pickings else {}

    return {
        "status": "ok",
        "order_number": order_number,
        "school": canonical_school,
        "website_group": website_name or None,
        "school_source_field": school_source_field,
        "sporthouse_status": picking.get("x_studio_estado_sporthouse") or None,
        "internal_delivery_state": picking.get("state") or None,
    }



_KNOWLEDGE_ARTICLE_CACHE = {"ts": 0.0, "items": []}
_KNOWLEDGE_ARTICLE_CACHE_TTL = 300
_EMBEDDING_CACHE = {}
_EMBEDDING_CACHE_MAX = 4000


def _load_knowledge_articles(uid, models):
    """Carga artículos de Knowledge una vez por TTL para que la búsqueda sea global y consistente."""
    now = time.time()
    if (
        _KNOWLEDGE_ARTICLE_CACHE["items"]
        and now - _KNOWLEDGE_ARTICLE_CACHE["ts"] < _KNOWLEDGE_ARTICLE_CACHE_TTL
    ):
        return _KNOWLEDGE_ARTICLE_CACHE["items"]

    rows = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "knowledge.article", "search_read",
        [[]],
        {
            "fields": ["id", "name", "body"],
            "order": "name asc",
            "limit": 1500,
        },
    ) or []

    cleaned = []
    for row in rows:
        cleaned.append({
            "id": row.get("id"),
            "name": (row.get("name") or "").strip(),
            "text": _clean_html(row.get("body") or ""),
        })

    _KNOWLEDGE_ARTICLE_CACHE.update({"ts": now, "items": cleaned})
    return cleaned


def _knowledge_scope_aliases(resolved_school: dict):
    """
    Devuelve los nombres que definen el universo permitido de conocimiento.
    Ejemplo IMS -> Instituto México Secundaria + Maristas.
    """
    aliases = []
    for value in (
        resolved_school.get("canonical_school"),
        resolved_school.get("parent_website"),
    ):
        value = (value or "").strip()
        if not value:
            continue
        if not any(_norm(value) == _norm(existing) for existing in aliases):
            aliases.append(value)
    return aliases


def _article_scope_score(article: dict, aliases: list[str]) -> float:
    """
    Decide si un artículo pertenece a la escuela/grupo sin depender de un catálogo de temas.
    Se apoya en el título y, como fallback, en menciones explícitas dentro del contenido.
    """
    title = article.get("name") or ""
    body = article.get("text") or ""
    title_norm = _norm(title)
    body_norm = _norm(body[:6000])
    best = 0.0

    for alias in aliases:
        if not alias:
            continue
        alias_norm = _norm(alias)
        score = _school_match_score(alias, title)
        if alias_norm and alias_norm in title_norm:
            score = max(score, 95.0)
        elif alias_norm and alias_norm in body_norm:
            score = max(score, 62.0)
        best = max(best, score)

    return best


def _chunk_text(text: str, max_chars: int = 1400, overlap: int = 180):
    """Divide artículos largos para recuperar el fragmento relevante, no el documento completo."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        chunk = text[start:end]

        # Intentar cortar cerca de un salto o final de frase para conservar legibilidad.
        if end < len(text):
            cut_candidates = [
                chunk.rfind("\n\n"),
                chunk.rfind("\n"),
                chunk.rfind(". "),
            ]
            cut = max(cut_candidates)
            if cut >= int(max_chars * 0.55):
                end = start + cut + (2 if chunk[cut:cut+2] == ". " else 0)
                chunk = text[start:end]

        chunk = chunk.strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)

    return chunks


def _normalize_vector(vector):
    norm = math.sqrt(sum(float(x) * float(x) for x in vector)) or 1.0
    return [float(x) / norm for x in vector]


def _embedding_cache_key(text: str) -> str:
    payload = f"{OPENAI_EMBEDDING_MODEL}\n{text}".encode("utf-8", errors="ignore")
    return hashlib.sha1(payload).hexdigest()


def _embed_texts(texts: list[str]):
    """
    Embeddings con caché en memoria. Si OpenAI no está disponible, el caller usa ranking léxico.
    """
    if not texts or not OPENAI_API_KEY:
        return [None] * len(texts)

    result = [None] * len(texts)
    missing_indexes = []
    missing_texts = []

    for idx, text in enumerate(texts):
        key = _embedding_cache_key(text)
        cached = _EMBEDDING_CACHE.get(key)
        if cached is not None:
            result[idx] = cached
        else:
            missing_indexes.append(idx)
            missing_texts.append(text)

    if missing_texts:
        client = OpenAI(api_key=OPENAI_API_KEY)
        # Batch corto para evitar requests gigantes si alguna escuela tiene mucho contenido.
        cursor = 0
        while cursor < len(missing_texts):
            batch_texts = missing_texts[cursor:cursor + 64]
            response = client.embeddings.create(
                model=OPENAI_EMBEDDING_MODEL,
                input=batch_texts,
            )
            for offset, item in enumerate(response.data):
                global_missing_pos = cursor + offset
                original_idx = missing_indexes[global_missing_pos]
                vector = _normalize_vector(item.embedding)
                result[original_idx] = vector
                _EMBEDDING_CACHE[_embedding_cache_key(texts[original_idx])] = vector
            cursor += len(batch_texts)

        # Límite sencillo de memoria; si crece demasiado, empezamos caché fresca.
        if len(_EMBEDDING_CACHE) > _EMBEDDING_CACHE_MAX:
            _EMBEDDING_CACHE.clear()

    return result


def _lexical_relevance(query: str, title: str, text: str) -> float:
    q_tokens = {
        token for token in re.findall(r"[a-z0-9]+", _norm(query))
        if len(token) >= 3
    }
    if not q_tokens:
        return 0.0
    title_norm = _norm(title)
    text_norm = _norm(text)
    score = 0.0
    for token in q_tokens:
        if token in title_norm:
            score += 3.0
        if token in text_norm:
            score += 0.7
    return score


def _semantic_knowledge_search(uid, models, resolved_school: dict, query: str, limit: int = 6):
    """
    Búsqueda semántica universal dentro del universo permitido de la escuela/grupo.
    No clasifica por temas: compara el significado de la pregunta contra fragmentos de todos
    los artículos relevantes de Odoo.
    """
    aliases = _knowledge_scope_aliases(resolved_school)
    all_articles = _load_knowledge_articles(uid, models)

    scoped = []
    for article in all_articles:
        scope_score = _article_scope_score(article, aliases)
        if scope_score >= 55:
            scoped.append((scope_score, article))

    if not scoped:
        return []

    chunk_rows = []
    for scope_score, article in scoped:
        chunks = _chunk_text(article.get("text") or "") or [""]
        for idx, chunk in enumerate(chunks):
            embedding_text = f"{article.get('name') or ''}\n{chunk}".strip()
            if not embedding_text:
                continue
            chunk_rows.append({
                "article_id": article.get("id"),
                "article_name": article.get("name"),
                "chunk_index": idx,
                "text": chunk,
                "embedding_text": embedding_text[:6000],
                "scope_score": scope_score,
            })

    if not chunk_rows:
        return []

    semantic_available = False
    query_vector = None
    chunk_vectors = [None] * len(chunk_rows)
    try:
        vectors = _embed_texts([query] + [row["embedding_text"] for row in chunk_rows])
        query_vector = vectors[0]
        chunk_vectors = vectors[1:]
        semantic_available = query_vector is not None and any(v is not None for v in chunk_vectors)
    except Exception:
        logger.warning("Fallo ranking semántico; usando fallback léxico", exc_info=True)

    ranked = []
    for idx, row in enumerate(chunk_rows):
        lexical = _lexical_relevance(query, row["article_name"], row["text"])
        semantic = 0.0
        if semantic_available and chunk_vectors[idx] is not None:
            semantic = sum(a * b for a, b in zip(query_vector, chunk_vectors[idx]))
        # Scope solo rompe empates; el significado de la pregunta debe dominar.
        final_score = (semantic * 100.0 if semantic_available else 0.0) + lexical + (row["scope_score"] / 100.0)
        ranked.append((final_score, semantic, lexical, row))

    ranked.sort(key=lambda x: (-x[0], _norm(x[3].get("article_name") or ""), x[3]["chunk_index"]))

    selected = []
    per_article = {}
    for final_score, semantic, lexical, row in ranked:
        article_id = row.get("article_id")
        # Máximo dos fragmentos del mismo artículo para dar diversidad de fuentes.
        if per_article.get(article_id, 0) >= 2:
            continue
        per_article[article_id] = per_article.get(article_id, 0) + 1
        selected.append({
            "article_id": article_id,
            "article_name": row.get("article_name"),
            "text": row.get("text"),
            "semantic_score": round(float(semantic), 4) if semantic_available else None,
        })
        if len(selected) >= limit:
            break

    return selected


def buscar_info_escuela(school: str, query: str):
    """
    Recupera semánticamente conocimiento de Odoo para la escuela concreta y su grupo.

    Esta herramienta es universal: sirve para cualquier información factual o de políticas
    que viva en Knowledge, sin requerir una ruta/intent específica por tema.
    """
    school = (school or "").strip()
    query = (query or "").strip()

    if not school:
        return {
            "status": "missing_school",
            "message": "Falta la escuela. Pregunta al cliente antes de buscar información específica.",
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
    knowledge_group = (resolved_school.get("parent_website") or "").strip() or None
    matches = _semantic_knowledge_search(uid, models, resolved_school, query, limit=6)

    if not matches:
        return {
            "status": "not_found",
            "school": canonical_school,
            "knowledge_group": knowledge_group,
            "query": query,
            "message": "No encontré información relevante en Knowledge para esa escuela o su grupo.",
        }

    return {
        "status": "ok",
        "school": canonical_school,
        "knowledge_group": knowledge_group,
        "query": query,
        "matches": matches,
        "instruction": (
            "Responde únicamente con información respaldada por estos fragmentos. "
            "Puedes combinar varios si son complementarios. Si los fragmentos no responden "
            "por completo, usa otras herramientas necesarias antes de concluir o escalar."
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
        return models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            model_name, "fields_get",
            [],
            {"attributes": ["type"]},
        ) or {}
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

    # La integración existente de SportHouse ya organiza los productos por categoría/escuela.
    # Eso actúa como frontera principal para no mezclar catálogos entre colegios.
    domain = [["categ_id.name", "ilike", canonical_school]]
    if "active" in product_fields:
        domain.append(["active", "=", True])
    if "sale_ok" in product_fields:
        domain.append(["sale_ok", "=", True])

    products = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.template", "search_read",
        [domain],
        {
            "fields": read_fields,
            "limit": 120,
            "order": "name asc",
        },
    )

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
    query_tokens = [t for t in re.findall(r"[a-z0-9]+", query_norm) if len(t) >= 2]

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

        category = product.get("categ_id")
        candidates.append({
            "name": name,
            "category": category[1] if isinstance(category, (list, tuple)) and len(category) > 1 else None,
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
        "total_products_in_school": len(products),
        "candidates": selected,
        "inventory_policy": (
            "No afirmar hay/no hay stock con esta herramienta. "
            "Dirigir al cliente al producto o a la tienda de la escuela para revisar opciones y comprar."
        ),
    }


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
            "Consulta en Odoo un pedido real. Llámala únicamente cuando conozcas la escuela "
            "y hayas resuelto con suficiente confianza un único número de pedido en formato S00000. "
            "No la llames si el cliente dio varios números, cree que le falta un dígito o existe ambigüedad: "
            "en esos casos conversa y pide aclaración primero."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "school": {
                    "type": "string",
                    "description": "Escuela confirmada por el cliente o ya conocida en el contexto.",
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
            "Busca semánticamente en toda la base Knowledge de Odoo permitida para la escuela y su grupo. "
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
que dependa de catálogo, producto, inventario, talla, precio, guía, política, entrega, FAQ o pedido debes
conocer primero la escuela. Nunca adivines la escuela por una prenda. Si no está clara, pregúntala de forma
natural. Si ya está confirmada en la conversación o en CONTEXTO EXTERNO, no la vuelvas a pedir. Si el cliente
cambia de escuela explícitamente, actualiza el contexto.

IMPORTANTE: escuela y sitio/grupo NO siempre son lo mismo. Una escuela concreta puede vivir dentro de un
website de grupo (por ejemplo, una escuela específica dentro de Maristas). Las herramientas pueden devolver
`school` como la escuela concreta y `website_group`/`parent_website` como el grupo. Cuando exista una escuela
específica en el pedido, esa escuela tiene prioridad para validar al cliente; el nombre del grupo por sí solo
puede ser insuficiente. Las herramientas resuelven abreviaciones y siglas contra nombres reales de escuela
configurados en Odoo. Cuando una herramienta devuelva un nombre canónico de escuela, consérvalo en `school`.
Si devuelve `specific_school_required`, pregunta la escuela concreta. Si devuelve `school_unresolved` o una
resolución ambigua, conversa para aclarar; no asumas otra escuela.

PEDIDOS
El formato canónico de SportHouse es S + 5 dígitos, por ejemplo S02038. El cliente NO tiene que escribirlo
perfectamente. Tú puedes comprender variantes de escritura y convertirlas al formato canónico cuando sea
inequívoco. Ejemplos de transformación inequívoca pueden ser mayúsculas/minúsculas, espacios, guiones,
una S omitida o un cero inicial omitido cuando el resto identifica claramente el mismo número.

La regla no es memorizar ejemplos: usa criterio conversacional.
- Si hay un único número inequívoco y conoces la escuela, llama consultar_pedido con S00000.
- Si hay varios números posibles, no elijas por el cliente: pregunta cuál quiere revisar.
- Si el cliente dice o sugiere que falta/sobra un dígito, o no puedes reconstruir un único S00000 con confianza,
  no inventes: pide que lo confirme.
- Si un número canónico no existe, pide verificarlo. No cambies dígitos para encontrar otro pedido.
- Si consultar_pedido devuelve school_mismatch, no reveles el estado ni otros datos del pedido.
- No necesitas validar el teléfono del cliente contra Odoo.
- Para responder estatus usa principalmente sporthouse_status. internal_delivery_state es un dato interno y
  no debe sustituir ni reinterpretar el Estado SportHouse ante el cliente.

FLUJO UNIVERSAL DE CONOCIMIENTO
Este criterio aplica a TODAS las preguntas de SportHouse, no a una lista cerrada de temas.
Una vez que la escuela esté clara, antes de responder una pregunta factual revisa el CONOCIMIENTO RECUPERADO
AUTOMÁTICAMENTE incluido en el contexto. Ese contenido fue seleccionado semánticamente desde Knowledge de Odoo
para la escuela concreta y, cuando corresponda, su grupo.

Si el conocimiento precargado no basta y la pregunta puede depender de información de SportHouse, llama
buscar_info_escuela con una consulta descriptiva de lo que necesitas saber. No esperes palabras exactas ni
clasifiques la pregunta en un catálogo rígido. La búsqueda es semántica.

Puedes y debes combinar Knowledge con herramientas operativas cuando sea necesario. Ejemplos conceptuales:
- un estatus de pedido puede requerir consultar_pedido + Knowledge para explicar qué sigue, dónde se entrega o tiempos;
- una pregunta de producto puede requerir buscar_producto_escuela + Knowledge para explicar cómo comprar;
- una pregunta de políticas puede resolverse solo con Knowledge.

El resultado de una herramienta NO significa automáticamente que la consulta completa esté resuelta. Antes de
responder, verifica si aún falta información relevante. Antes de escalar por falta de información, si la escuela
está clara y existe una posibilidad razonable de que la respuesta viva en Knowledge, consulta Knowledge primero.
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
recibir el historial del hilo mediante previous_response_id. Conserva en tu salida los datos ya confirmados,
salvo que el cliente los corrija o cambie explícitamente. No guardes como definitivo un dato que tú mismo
consideras ambiguo.

ESCALAMIENTO
No digas "ya te pasé con un asesor" a menos que realmente hayas llamado escalar_asesor. Si lo llamas,
puedes decir que un asesor continuará o revisará el caso. No escales solo porque el cliente escribió raro:
primero conversa y aclara cuando sea razonable. Tampoco escales por falta de información factual sin haber
revisado el conocimiento precargado y, cuando corresponda, haber llamado buscar_info_escuela.

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
    return f"""
CONTEXTO EXTERNO ACTUAL (puede contener valores vacíos):
- escuela confirmada: {data.school or 'NO CONOCIDA'}
- pedido confirmado: {data.order_number or 'NO CONOCIDO'}
- producto: {data.product or 'NO CONOCIDO'}
- talla: {data.size or 'NO CONOCIDA'}
- necesidad/intención previa: {data.intent or 'NO CONOCIDA'}
- nombre del cliente: {data.first_name or 'NO DISPONIBLE'}

CONOCIMIENTO RECUPERADO AUTOMÁTICAMENTE PARA ESTE TURNO:
{_format_prefetched_knowledge(prefetched_knowledge)}

MENSAJE NUEVO DEL CLIENTE:
{data.message.strip()}
""".strip()


def _fallback_context(data: ChatRequest):
    return {
        "school": data.school or None,
        "order_number": data.order_number or None,
        "product": data.product or None,
        "size": data.size or None,
        "intent": data.intent or None,
    }


def _parse_final_response(response, data: ChatRequest, needs_human_from_tool=False, escalation_reason_from_tool=None):
    raw = (response.output_text or "").strip()
    parsed = json.loads(raw)

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


@router.post("/chat")
async def chat(data: ChatRequest):
    if not data.message or not data.message.strip():
        return {
            "reply": "¿En qué puedo ayudarte? 😊",
            **_fallback_context(data),
            "needs_human": False,
            "escalation_reason": None,
            "response_id": data.previous_response_id,
            "tools_used": [],
        }

    if not OPENAI_API_KEY:
        return {
            "reply": "En este momento no puedo procesar tu mensaje. Un asesor de SportHouse puede ayudarte.",
            **_fallback_context(data),
            "needs_human": True,
            "escalation_reason": "openai_not_configured",
            "response_id": data.previous_response_id,
            "tools_used": [],
        }

    client = OpenAI(api_key=OPENAI_API_KEY)
    tools_used = []
    knowledge_sources = []
    needs_human_from_tool = False
    escalation_reason_from_tool = None

    # Flujo universal: si ManyChat ya conoce la escuela, recuperamos Knowledge semánticamente
    # ANTES de que el agente decida qué herramientas operativas necesita. Esto aplica a cualquier
    # pregunta factual y evita depender de que el modelo recuerde llamar una herramienta por tema.
    prefetched_knowledge = None
    if data.school:
        try:
            prefetched_knowledge = buscar_info_escuela(
                school=data.school,
                query=data.message,
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

    try:
        create_args = {
            "model": OPENAI_MODEL,
            "instructions": INSTRUCTIONS,
            "input": [{"role": "user", "content": _external_context(data, prefetched_knowledge)}],
            "tools": TOOLS,
            "tool_choice": "auto",
            "text": {"format": FINAL_RESPONSE_FORMAT},
            "max_output_tokens": 700,
        }
        if data.previous_response_id:
            create_args["previous_response_id"] = data.previous_response_id

        try:
            response = client.responses.create(**create_args)
        except Exception:
            # Si un hilo previo expiró o dejó de estar disponible, seguimos con los
            # custom fields de ManyChat en vez de romper la conversación.
            if not data.previous_response_id:
                raise
            logger.warning("No se pudo continuar previous_response_id; reiniciando hilo", exc_info=True)
            create_args.pop("previous_response_id", None)
            response = client.responses.create(**create_args)

        # El modelo puede encadenar varias herramientas en un mismo turno.
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
                return result

            tool_outputs = []
            for call in function_calls:
                try:
                    args = json.loads(call.arguments or "{}")
                except Exception:
                    args = {}

                tools_used.append(call.name)

                if call.name == "consultar_pedido":
                    tool_result = consultar_pedido(
                        school=args.get("school"),
                        order_number=args.get("order_number"),
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

                tool_outputs.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(tool_result, ensure_ascii=False),
                })

            # Continuar exactamente el hilo generado por el modelo, incluyendo sus
            # tool calls. El modelo recibe datos; él decide cómo interpretarlos y responder.
            response = client.responses.create(
                model=OPENAI_MODEL,
                instructions=INSTRUCTIONS,
                previous_response_id=response.id,
                input=tool_outputs,
                tools=TOOLS,
                tool_choice="auto",
                text={"format": FINAL_RESPONSE_FORMAT},
                max_output_tokens=700,
            )

        return {
            "reply": "Necesito que un asesor de SportHouse continúe contigo para revisar este caso.",
            **_fallback_context(data),
            "needs_human": True,
            "escalation_reason": "tool_loop_limit",
            "response_id": response.id,
            "tools_used": tools_used,
            "knowledge_sources": list(dict.fromkeys(knowledge_sources)),
        }

    except Exception:
        logger.exception("Error en POST /chat")
        return {
            "reply": "Tuve un problema al revisar la información. Un asesor de SportHouse puede ayudarte.",
            **_fallback_context(data),
            "needs_human": True,
            "escalation_reason": "backend_error",
            "response_id": data.previous_response_id,
            "tools_used": tools_used,
            "knowledge_sources": list(dict.fromkeys(knowledge_sources)),
        }
