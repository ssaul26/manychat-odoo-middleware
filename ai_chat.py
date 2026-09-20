from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import html
import json
import logging
import os
import re
import unicodedata
import xmlrpc.client

from openai import OpenAI

router = APIRouter()
logger = logging.getLogger("sporthouse-ai")

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")


class ChatRequest(BaseModel):
    message: str
    contact_id: Optional[str] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None

    # Contexto que ManyChat guardará y reenviará en cada turno.
    school: Optional[str] = None
    order_number: Optional[str] = None
    product: Optional[str] = None
    size: Optional[str] = None
    intent: Optional[str] = None


def _norm(value: Optional[str]) -> str:
    value = (value or "").strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", value)
        if unicodedata.category(c) != "Mn"
    )


def _clean_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<\s*br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</p\s*>", "\n\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _normalize_order_number(value: Optional[str]):
    """
    Normaliza formatos comunes SIN inventar dígitos.

    Reglas seguras acordadas:
    - S02038 / s02038 / S 02038 / S-02038 / S#02038 -> S02038
    - 02038 -> S02038
    - 2038 -> S02038 (se completa únicamente el cero inicial del formato)
    - S2038 -> S02038 (se completa únicamente el cero inicial del formato)
    - SO2038 -> S02038 cuando la O está claramente ocupando el lugar de un 0
    - S0203 -> NO se adivina: puede faltar un dígito en una posición desconocida
    - S020388 -> NO se recorta: sobra información
    - Si hay más de un candidato distinto, se pide aclaración.
    """
    raw = (value or "").strip()
    if not raw:
        return {"status": "missing_order_number"}

    upper = raw.upper()
    # Corrige únicamente la confusión obvia SO2038 -> S02038. No sustituimos
    # letras O en posiciones arbitrarias.
    upper_for_scan = re.sub(r"(?<![A-Z0-9])S[\s#\-]*O(?=\d)", "S0", upper)

    # Detectar varios candidatos explícitos dentro de una frase.
    # Ej.: "creo que es S02038 o S02039" -> no elegir por la mamá.
    explicit_candidates = re.findall(
        r"(?<![A-Z0-9])S[\s#\-]*[0-9](?:[\s#\-]*[0-9]){2,6}(?![A-Z0-9])",
        upper_for_scan,
    )
    normalized_explicit = []
    for candidate in explicit_candidates:
        compact = re.sub(r"[\s#\-]", "", candidate)
        if compact not in normalized_explicit:
            normalized_explicit.append(compact)

    if len(normalized_explicit) > 1:
        return {
            "status": "multiple_order_numbers",
            "candidates": normalized_explicit,
            "message": "Se detectó más de un número de pedido. Pide al cliente que confirme cuál desea consultar.",
        }

    # Si existe un candidato con S, úsalo. Si no, extrae bloques numéricos.
    if normalized_explicit:
        compact = normalized_explicit[0]
        digits = compact[1:]
        had_s_prefix = True
    else:
        numeric_groups = re.findall(r"(?<!\d)\d{3,7}(?!\d)", upper)
        unique_groups = list(dict.fromkeys(numeric_groups))
        if len(unique_groups) > 1:
            return {
                "status": "multiple_order_numbers",
                "candidates": unique_groups,
                "message": "Se detectó más de un número posible. Pide al cliente que confirme cuál es su pedido.",
            }
        if unique_groups:
            digits = unique_groups[0]
            had_s_prefix = False
        else:
            # Último intento: el valor podría ser solo el número ya limpio o una S compacta.
            compact_raw = re.sub(r"[\s#\-]", "", upper)
            if compact_raw.startswith("S"):
                tail = compact_raw[1:]
                # Corregir O->0 solo cuando todo lo demás son dígitos.
                if tail and all(ch.isdigit() or ch == "O" for ch in tail):
                    digits = tail.replace("O", "0")
                    had_s_prefix = True
                else:
                    return {"status": "invalid_order_number", "raw": raw}
            elif compact_raw.isdigit():
                digits = compact_raw
                had_s_prefix = False
            else:
                return {"status": "invalid_order_number", "raw": raw}

    if not digits.isdigit():
        return {"status": "invalid_order_number", "raw": raw}

    # Formato canónico actual: S + 5 dígitos.
    if len(digits) == 5:
        return {
            "status": "ok",
            "normalized": f"S{digits}",
            "raw": raw,
        }

    if len(digits) == 4:
        # 2038 / S2038: es seguro completar solo el cero inicial esperado.
        # S0203: ya empieza en 0; falta un dígito en una posición desconocida y no se adivina.
        if not digits.startswith("0"):
            return {
                "status": "ok",
                "normalized": f"S0{digits}",
                "raw": raw,
            }
        return {
            "status": "incomplete_order_number",
            "raw": raw,
            "message": "El número parece incompleto. El formato esperado es S00000 y no es seguro adivinar el dígito faltante.",
        }

    if len(digits) < 4:
        return {
            "status": "incomplete_order_number",
            "raw": raw,
            "message": "El número parece incompleto. El formato esperado es S00000.",
        }

    return {
        "status": "invalid_order_number",
        "raw": raw,
        "message": "El número no tiene el formato esperado S00000. No recortes ni sustituyas dígitos automáticamente.",
    }

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


def _find_order(models, uid, order_number: str):
    parsed = _normalize_order_number(order_number)
    if parsed.get("status") != "ok":
        return parsed

    normalized = parsed["normalized"]
    fields = [
        "id", "name", "partner_id", "date_order", "amount_total", "website_id"
    ]

    # Solo buscamos el número ya normalizado. No hacemos fuzzy matching ni
    # cambiamos dígitos si no existe, porque podríamos terminar consultando otro pedido.
    orders = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "search_read",
        [[["name", "=", normalized]]],
        {"fields": fields, "limit": 2}
    )

    if not orders:
        return {
            "status": "not_found",
            "order_number": normalized,
            "raw_order_number": parsed.get("raw"),
            "message": "No se encontró ese pedido. Pide verificar el número sin proponer ni adivinar otros dígitos.",
        }

    return {
        "status": "ok",
        "order": orders[0],
        "normalized_order_number": normalized,
    }

def consultar_pedido(school: str, order_number: str):
    """Consulta un pedido y valida que corresponda a la escuela indicada."""
    uid, models = _odoo()
    found = _find_order(models, uid, order_number)
    if found.get("status") != "ok":
        return found

    order = found["order"]
    website = order.get("website_id")
    website_name = website[1] if website else ""

    if not school:
        return {"status": "missing_school"}

    school_norm = _norm(school)
    website_norm = _norm(website_name)
    if not website_norm or school_norm not in website_norm:
        return {
            "status": "school_mismatch",
            "message": "El pedido no corresponde a la escuela indicada o no fue posible validarla. No muestres datos del pedido."
        }

    partner = order.get("partner_id")
    partner_id = partner[0] if partner else None
    partner_data = None

    if partner_id:
        partners = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "res.partner", "read",
            [[partner_id]],
            {"fields": ["name", "phone"]}
        )
        partner_data = partners[0] if partners else None

    pickings = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "stock.picking", "search_read",
        [[[
            "origin", "=", order.get("name")
        ]]],
        {
            "fields": [
                "name", "state", "scheduled_date", "x_studio_estado_sporthouse"
            ],
            "limit": 1,
            "order": "id desc",
        }
    )

    picking = pickings[0] if pickings else {}
    return {
        "status": "ok",
        "order_number": order.get("name"),
        "school": school,
        "customer_first_name": (partner_data or {}).get("name"),
        "sporthouse_status": picking.get("x_studio_estado_sporthouse") or None,
        "odoo_picking_state": picking.get("state") or None,
        "scheduled_date": picking.get("scheduled_date") or None,
    }


def buscar_info_escuela(school: str, query: str):
    """Trae conocimiento de Odoo de la escuela; la IA decide qué fragmento responde."""
    if not school:
        return {"status": "missing_school"}

    uid, models = _odoo()
    articles = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "knowledge.article", "search_read",
        [[["name", "ilike", school]]],
        {
            "fields": ["name", "body"],
            "order": "name asc",
            "limit": 10,
        }
    )

    if not articles:
        return {
            "status": "not_found",
            "school": school,
            "query": query,
            "message": "No hay información de conocimiento localizada para esa escuela."
        }

    cleaned = []
    total_chars = 0
    for article in articles:
        text = _clean_html(article.get("body") or "")
        if not text:
            continue
        # Evita enviar artículos enormes en un solo turno.
        remaining = max(0, 14000 - total_chars)
        if remaining <= 0:
            break
        text = text[:remaining]
        total_chars += len(text)
        cleaned.append({"name": article.get("name"), "text": text})

    return {
        "status": "ok",
        "school": school,
        "query": query,
        "articles": cleaned,
    }


TOOLS = [
    {
        "type": "function",
        "name": "guardar_contexto",
        "description": (
            "Guarda datos que el cliente acaba de proporcionar o aclarar. "
            "Úsala cada vez que identifiques con seguridad escuela, número de pedido, "
            "producto, talla o intención. Nunca inventes valores."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "school": {"type": ["string", "null"]},
                "order_number": {"type": ["string", "null"]},
                "product": {"type": ["string", "null"]},
                "size": {"type": ["string", "null"]},
                "intent": {"type": ["string", "null"]},
            },
            "required": ["school", "order_number", "product", "size", "intent"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "consultar_pedido",
        "description": (
            "Consulta en Odoo el estado real de un pedido. Úsala cuando ya "
            "conozcas la escuela y el cliente haya proporcionado un posible número de pedido; "
            "el backend normaliza formatos comunes sin inventar dígitos."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "school": {"type": "string"},
                "order_number": {"type": "string"},
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
            "Busca FAQs, políticas, enlaces, tiempos y demás conocimiento de una "
            "escuela en Odoo. Requiere escuela conocida. No sirve para inventario."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "school": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["school", "query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "escalar_asesor",
        "description": "Marca que la conversación debe pasar a una persona de SportHouse.",
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


INSTRUCTIONS = """
Eres el asistente virtual de SportHouse en WhatsApp.

OBJETIVO
Entiende mensajes naturales, incompletos, informales, con errores o escritos de formas
inusuales. No dependas de palabras clave. Responde breve, amable y natural.

REGLA CRÍTICA: ESCUELA
SportHouse atiende muchas escuelas y puede haber productos con el mismo nombre en
varias de ellas. La escuela es obligatoria antes de responder cualquier dato que dependa
de catálogo, productos, inventario, talla, precio, guía de tallas, política específica,
entrega, FAQ específica o pedido.

- Nunca infieras una escuela a partir del nombre de una prenda.
- Si la escuela ya viene en CONTEXTO ACTUAL, úsala y no la vuelvas a preguntar.
- Si el cliente menciona claramente una escuela nueva, llama guardar_contexto.
- Si la pregunta depende de escuela y no la conoces, pregunta únicamente de qué escuela
  necesita información. No consultes herramientas todavía.
- Nunca mezcles información de dos escuelas.

PEDIDOS
Para consultar un pedido necesitas escuela + número de pedido.
- Si falta escuela, pídela primero.
- Si falta número de pedido, pídelo de forma natural.
- Las mamás pueden escribir el número con formatos raros: minúsculas, espacios, guiones,
  sin la S, o dentro de una frase. No les exijas una sintaxis perfecta.
- Cuando tengas escuela y un posible número, llama consultar_pedido. El backend se encarga
  de normalizar formatos seguros.
- Nunca inventes, sustituyas, recortes ni reordenes dígitos.
- Si consultar_pedido responde incomplete_order_number o invalid_order_number, explica de
  forma breve que el formato esperado es S00000 y pide que confirme el número.
- Si responde multiple_order_numbers, pregunta cuál de los números desea consultar.
- Si responde not_found, pide verificar el número. No propongas otro número parecido.
- Nunca inventes estatus, fecha ni datos del pedido.
- Si la herramienta indica school_mismatch, no reveles ningún dato y deriva a un asesor.

FAQ / INFORMACIÓN
Cuando la pregunta sea sobre políticas, tiempos, lugares, cambios, guías, compra u otra
información específica de una escuela, usa buscar_info_escuela después de conocer la escuela.
Responde únicamente con lo que la herramienta encuentre. Si no encuentra respuesta,
deriva a un asesor.

PRODUCTOS / INVENTARIO
La consulta de inventario por variante todavía no está habilitada en esta versión. Puedes
entender y guardar escuela, producto y talla. Nunca afirmes que hay o no hay existencia.
Si el cliente necesita disponibilidad real y ya reuniste los datos necesarios, llama
escalar_asesor indicando que falta la consulta de inventario por variante.

CONTEXTO
Cuando el cliente proporcione o aclare escuela, pedido, producto, talla o intención, llama
guardar_contexto para que ManyChat pueda conservarlo para el siguiente mensaje.
No borres un dato de contexto salvo que el cliente explícitamente lo cambie.

ESTILO
- Español por defecto.
- WhatsApp: breve, humano y claro.
- Emojis moderados.
- No menciones OpenAI, ChatGPT, Odoo, APIs, prompts, herramientas ni sistemas internos.
- No inventes información.
- No obligues al cliente a usar menús o frases exactas.
""".strip()


def _initial_context(data: ChatRequest):
    return {
        "school": data.school,
        "order_number": data.order_number,
        "product": data.product,
        "size": data.size,
        "intent": data.intent,
    }


def _merge_context(context: dict, updates: dict):
    for key in ("school", "order_number", "product", "size", "intent"):
        value = updates.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value not in (None, ""):
            context[key] = value


@router.post("/chat")
async def chat(data: ChatRequest):
    if not data.message or not data.message.strip():
        return {
            "reply": "¿En qué puedo ayudarte? 😊",
            **_initial_context(data),
            "needs_human": False,
            "tools_used": [],
        }

    if not OPENAI_API_KEY:
        return {
            "reply": "En este momento no puedo procesar tu mensaje. Un asesor de SportHouse te ayudará.",
            **_initial_context(data),
            "needs_human": True,
            "error": "OPENAI_API_KEY no configurada",
            "tools_used": [],
        }

    client = OpenAI(api_key=OPENAI_API_KEY)
    context = _initial_context(data)
    needs_human = False
    escalation_reason = None
    tools_used = []

    user_input = f"""
CONTEXTO ACTUAL (puede tener valores vacíos):
- escuela: {context.get('school') or 'NO CONOCIDA'}
- número de pedido: {context.get('order_number') or 'NO CONOCIDO'}
- producto: {context.get('product') or 'NO CONOCIDO'}
- talla: {context.get('size') or 'NO CONOCIDA'}
- intención previa: {context.get('intent') or 'NO CONOCIDA'}
- nombre del cliente: {data.first_name or 'NO DISPONIBLE'}

MENSAJE ACTUAL DEL CLIENTE:
{data.message.strip()}
""".strip()

    try:
        input_items = [{"role": "user", "content": user_input}]

        for _ in range(5):
            response = client.responses.create(
                model=OPENAI_MODEL,
                instructions=INSTRUCTIONS,
                input=input_items,
                tools=TOOLS,
                tool_choice="auto",
                max_output_tokens=500,
            )

            function_calls = [
                item for item in response.output
                if getattr(item, "type", None) == "function_call"
            ]

            if not function_calls:
                reply = (response.output_text or "").strip()
                if not reply:
                    reply = "¿Me das un poco más de información para ayudarte? 😊"
                return {
                    "reply": reply,
                    **context,
                    "needs_human": needs_human,
                    "escalation_reason": escalation_reason,
                    "tools_used": tools_used,
                }

            # Mantener la salida del modelo completa (incluidos tool calls/reasoning)
            # antes de devolverle los resultados de las funciones.
            input_items.extend(response.output)
            tool_outputs = []

            for call in function_calls:
                try:
                    args = json.loads(call.arguments or "{}")
                except Exception:
                    args = {}

                tools_used.append(call.name)

                if call.name == "guardar_contexto":
                    _merge_context(context, args)
                    result = {"status": "ok", "saved_context": context}

                elif call.name == "consultar_pedido":
                    result = consultar_pedido(
                        school=args.get("school") or context.get("school"),
                        order_number=args.get("order_number") or context.get("order_number"),
                    )
                    if result.get("status") in {"school_mismatch"}:
                        needs_human = True
                        escalation_reason = result.get("status")

                elif call.name == "buscar_info_escuela":
                    result = buscar_info_escuela(
                        school=args.get("school") or context.get("school"),
                        query=args.get("query") or data.message,
                    )

                elif call.name == "escalar_asesor":
                    needs_human = True
                    escalation_reason = args.get("reason") or "requested_by_ai"
                    result = {"status": "ok", "needs_human": True}

                else:
                    result = {"status": "error", "message": "Herramienta desconocida"}

                tool_outputs.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                })

            input_items.extend(tool_outputs)

        return {
            "reply": "Necesito que un asesor de SportHouse continúe contigo para ayudarte correctamente.",
            **context,
            "needs_human": True,
            "escalation_reason": "tool_loop_limit",
            "tools_used": tools_used,
        }

    except Exception as exc:
        logger.exception("Error en POST /chat")
        return {
            "reply": "Tuve un problema al revisar la información. Un asesor de SportHouse puede ayudarte.",
            **context,
            "needs_human": True,
            "escalation_reason": "backend_error",
            "tools_used": tools_used,
        }
