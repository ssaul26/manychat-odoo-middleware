from fastapi import FastAPI, Query, Request
import xmlrpc.client
import os
from collections import defaultdict, OrderedDict
from datetime import datetime
import unicodedata, time, re
import logging
import requests

app = FastAPI()

# 🔐 Variables de entorno
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# Logger opcional para depuración
logger = logging.getLogger("middleware")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


@app.get("/")
def root():
    return {"status": "✅ API funcionando correctamente 💫"}


@app.get("/inventario")
def get_inventario(
    limit: int = Query(5, ge=1, le=50, description="Número de productos a devolver"),
    offset: int = Query(0, ge=0, description="Desplazamiento (paginación)"),
    category: str = Query(None, description="Filtrar por categoría (opcional)"),
    format: str = Query("json", regex="^(json|text)$", description="json (default) o text")
):
    """
    Devuelve productos a nivel product.template (1 por producto).
    Incluye TODOS los atributos del template (ej. Tipo de tela, Sexo, Color, Talla).
    - format=json  -> {"productos":[...], "next_offset": <int>}
    - format=text  -> {"catalogo_msg": "...", "next_offset": <int>}
    """
    try:
        # 1) Autenticación
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return {"catalogo_msg": "❌ Error de autenticación con Odoo. Verifica credenciales.", "next_offset": 0}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # 2) Dominio de búsqueda
        domain = [['active', '=', True]]
        # domain.append(['sale_ok', '=', True])  # Opcional: solo vendibles
        if category:
            domain.append(['categ_id.name', 'ilike', category])

        # 3) Leer templates
        templates = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.template', 'search_read',
            [domain],
            {
                'fields': [
                    'id', 'name', 'list_price', 'qty_available',
                    'categ_id', 'sale_ok'
                ],
                'limit': limit,
                'offset': offset,
                'order': 'name asc',
            }
        )

        # 4) Normalización + TODOS los atributos del template
        PREFERRED_ORDER = ["Tipo de tela", "Sexo", "Color", "Talla"]  # orden visual sugerido

        def normalize_template(t):
            atributos = defaultdict(list)
            try:
                ptavs = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'product.template.attribute.value', 'search_read',
                    [[['product_tmpl_id', '=', t['id']]]],
                    {'fields': ['attribute_id', 'name']}
                )
                for v in ptavs:
                    attr_name = v['attribute_id'][1] if v.get('attribute_id') else None
                    val_name = v.get('name')
                    if attr_name and val_name and val_name not in atributos[attr_name]:
                        atributos[attr_name].append(val_name)
            except Exception:
                pass

            # Ordenar atributos: preferidos primero, luego alfabético
            ordered = OrderedDict()
            for key in PREFERRED_ORDER:
                if key in atributos:
                    ordered[key] = atributos[key]
            for k in sorted(atributos.keys()):
                if k not in ordered:
                    ordered[k] = atributos[k]

            return {
                "id": t["id"],
                "name": t.get("name"),
                "price": t.get("list_price"),
                "stock": t.get("qty_available"),   # numérico (suma de variantes)
                "template": t.get("name"),
                "category": t["categ_id"][1] if t.get("categ_id") else None,
                "attributes": ordered,
                "sku": None,
                "barcode": None
            }

        items = [normalize_template(t) for t in templates]

        # 5) Paginación
        next_offset = (offset + limit) if len(items) == limit else 0

        # 6) Salidas
        if format == "json":
            # En JSON mantenemos cantidad numérica
            return {"productos": items, "next_offset": next_offset}

        # ---- format == "text" (para ManyChat) ----
        header = f"🌿 *Catálogo para {category or 'tu selección'}* 🌸\n"
        if not items:
            return {"catalogo_msg": header + "No encontramos productos por ahora. 🙈", "next_offset": 0}

        bloques = []
        for it in items:
            stock_qty = int(it.get('stock') or 0)
            stock_label = "DISPONIBLE" if stock_qty > 0 else "NO DISPONIBLE"

            lineas = [
                f"⭐ *{it.get('name') or 'Producto'}*",
                f"💰 Precio: ${it.get('price') or 0}",
                f"📦 {stock_label}",
            ]
            attrs = it.get("attributes") or {}
            for attr_name, values in attrs.items():
                if values:
                    lineas.append(f"• {attr_name}: {', '.join(values)}")
            bloques.append("\n".join(lineas))

        catalogo_msg = header + "\n" + "\n\n".join(bloques)
        return {"catalogo_msg": catalogo_msg, "next_offset": next_offset}

    except Exception as e:
        return {"catalogo_msg": f"⚠️ Hubo un error obteniendo el catálogo.\n\nDetalle: {str(e)}", "next_offset": 0}


@app.get("/faq")
def get_faq(category: str = None, format: str = "text"):
    """
    Devuelve FAQs con formato limpio para ManyChat (sin BeautifulSoup).
    """
    import re, html

    try:
        # --- Autenticación ---
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return {"faq_msg": "❌ Error de autenticación con Odoo."}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # --- Dominio ---
        domain = []
        if category:
            domain.append(["name", "ilike", category])

        # --- Consulta ---
        faq_records = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "knowledge.article", "search_read",
            [domain],
            {"fields": ["name", "body"], "order": "name asc", "limit": 10}
        )

        if not faq_records:
            return {"faq_msg": f"⚠️ No se encontraron artículos para '{category}'."}

        # --- Limpieza y formato (sin BeautifulSoup) ---
        def clean_html(text):
            text = html.unescape(text or "")
            text = re.sub(r"<\s*br\s*/?>", "\n", text)  # <br> → salto
            text = re.sub(r"</p\s*>", "\n\n", text)     # </p> → doble salto
            text = re.sub(r"<[^>]+>", "", text)         # elimina etiquetas restantes
            text = text.replace("\xa0", " ")
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text.strip()

        bloques = []
        for rec in faq_records:
            name = rec.get("name", "Preguntas Frecuentes")
            body = rec.get("body", "")
            texto = clean_html(body)
            # Detecta preguntas (terminan con ?)
            texto = re.sub(r"([^\n]*\?)", r"\n💬 *\1*\n", texto)
            texto = re.sub(r"\n{3,}", "\n\n", texto)  # compactar saltos
            bloque = f"\n{texto}\n\n"
            bloques.append(bloque)

        faq_msg = "\n".join(bloques).strip()
        return {"faq_msg": faq_msg, "total": len(bloques)}

    except Exception as e:
        return {"faq_msg": f"⚠️ Error al procesar las FAQ: {str(e)}"}


def normalize_datetime(s: str | None) -> str:
    if not s:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    # Intenta varios formatos comunes (incluye el de ManyChat: '20 Oct 2025, 05:41pm')
    for parser in (
        lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),        # ISO
        lambda x: datetime.strptime(x, "%d %b %Y, %I:%M%p"),               # 20 Oct 2025, 05:41pm
        lambda x: datetime.strptime(x, "%d %B %Y, %I:%M%p"),               # 20 October 2025, 05:41pm
        lambda x: datetime.strptime(x, "%Y-%m-%d %H:%M:%S"),               # 2025-10-20 17:41:00
        lambda x: datetime.strptime(x, "%Y-%m-%dT%H:%M:%S.%f"),            # 2025-10-20T17:41:00.123456
        lambda x: datetime.strptime(x, "%Y-%m-%dT%H:%M:%S"),               # 2025-10-20T17:41:00
    ):
        try:
            dt = parser(s)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


@app.post("/register_interaction")
async def register_interaction(request: Request):
    try:
        data = await request.json()

        messenger_id = (data.get("messenger_id") or "").strip()
        canal        = (data.get("canal") or "").strip()
        evento       = (data.get("evento") or "").strip()
        fecha_norm   = normalize_datetime(data.get("fecha"))
        telefono     = (data.get("telefono") or "").strip()
        correo       = (data.get("correo") or "").strip()

        if not messenger_id:
            return {"status": "error", "message": "Falta messenger_id"}

        # 1) Autenticación
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return {"status": "error", "message": "❌ Error de autenticación en Odoo."}
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # 2) Construimos valores a guardar (ajusta nombres técnicos si difieren)
        vals = {
            "x_name": f"{canal or 'Canal'} - {evento or 'Interacción'} - {messenger_id}",
            "x_studio_messeger_id": messenger_id,   # si en Studio es x_studio_messenger_id, cambia aquí
            "x_studio_channel":     canal,
            "x_studio_event":       evento,
            "x_studio_timestamp":   fecha_norm,
            "x_studio_phone":       telefono,
            "x_studio_email":       correo,
        }

        # 3) DEDUPE / UPSERT
        domain = []
        if messenger_id:
            domain = [["x_studio_messeger_id", "=", messenger_id]]
        elif correo:
            domain = [["x_studio_email", "=", correo]]
        elif telefono:
            domain = [["x_studio_phone", "=", telefono]]

        existing_ids = []
        if domain:
            existing_ids = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "x_interacciones_chatbo", "search",
                [domain], {"limit": 1}
            )

        if existing_ids:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "x_interacciones_chatbo", "write",
                [existing_ids, vals]
            )
            rec_id = existing_ids[0]
            action = "updated"
        else:
            rec_id = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "x_interacciones_chatbo", "create",
                [vals]
            )
            action = "created"

        return {"status": "success", "action": action, "record_id": rec_id}

    except Exception as e:
        return {"status": "error", "message": f"Error al registrar interacción: {str(e)}"}


# --- NUEVO: helpers para formato ---
def _format_money(amount: float, symbol: str = os.getenv("CURRENCY_SYMBOL", "$")) -> str:
    try:
        return f"{symbol} {float(amount):,.2f}"
    except Exception:
        return f"{symbol} {amount}"

def _format_odoo_datetime(s: str | None) -> str:
    """
    sale.order.date_order suele venir como 'YYYY-MM-DD HH:MM:SS' (UTC).
    Dejamos un formato simple dd/mm/YYYY HH:MM sin timezone.
    """
    if not s:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%d/%m/%Y %H:%M") if fmt.endswith("%S") else dt.strftime("%d/%m/%Y")
        except Exception:
            continue
    return s  # si no se pudo parsear, regresa crudo


@app.post("/order_lookup")
async def order_lookup(request: Request):
    try:
        data = await request.json()
        order_number = (data.get("order_number") or "").strip()
        if not order_number:
            return {
                "found": False,
                "mc_message": "⚠️ Proporciona el número de pedido (por ejemplo: S00413)."
            }

        # 1) Autenticación con Odoo
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return {"found": False, "mc_message": "❌ Error de autenticación con Odoo."}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # 2) Buscar sale.order por nombre exacto
        domain = [["name", "=", order_number]]
        fields = ["name", "partner_id", "date_order", "amount_total"]
        so = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "search_read",
            [domain],
            {"fields": fields, "limit": 1}
        )

        if not so:
            return {
                "found": False,
                "order_number": order_number,
                "mc_message": f"😕 No encontré el pedido {order_number}. Verifica el formato (ejemplo: S00413)."
            }

        o = so[0]
        client_name = (o.get("partner_id") or ["", ""])[1]
        order_date = _format_odoo_datetime(o.get("date_order"))
        order_total = _format_money(o.get("amount_total") or 0.0)

        # 3) Buscar el stock.picking relacionado
        picking_domain = [
            ["origin", "=", o["name"]]
        ]
        picking_fields = [
            "name",
            "origin",
            "state",
            "scheduled_date",
            "x_studio_estado_sporthouse"
        ]

        pickings = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "stock.picking", "search_read",
            [picking_domain],
            {
                "fields": picking_fields,
                "limit": 1,
                "order": "id desc"
            }
        )

        sporthouse_status = ""
        picking_name = ""
        picking_state = ""

        if pickings:
            p = pickings[0]
            sporthouse_status = p.get("x_studio_estado_sporthouse") or ""
            picking_name = p.get("name") or ""
            picking_state = p.get("state") or ""

        # 4) Mensaje formateado
        estado_txt = sporthouse_status if sporthouse_status else "Sin estatus disponible"

        mc_message = (
            f"👋 ¡Hola {client_name}!\n\n"
            f"📦 Tu pedido *{o['name']}* se realizó el {order_date} "
            f"por un total de *{order_total}*.\n\n"
            f"🏷️ Estatus actual: *{estado_txt}*\n\n"
            f"🚚 Si tienes dudas sobre tiempos o formas de entrega, "
            f"consulta nuestro apartado de *Preguntas Frecuentes*.\n\n"
            f"¡Gracias por tu compra con *Sporthouse*!"
        )

        return {
            "found": True,
            "client_name": client_name,
            "order_number": o["name"],
            "order_date": order_date,
            "order_total": order_total,
            "sporthouse_status": sporthouse_status,
            "picking_name": picking_name,
            "picking_state": picking_state,
            "mc_message": mc_message
        }

    except Exception as e:
        return {"found": False, "mc_message": f"⚠️ Error al consultar pedido: {str(e)}"}

# ======== INTENT RULES Y NLP ========

_INTENT_CACHE = {}          # cache por escuela
_INTENT_CACHE_TTL = 300     # 5 minutos

def _norm(s: str) -> str:
    s = (s or "").lower().strip()
    return ''.join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _load_rules(school: str):
    """
    Lee reglas desde Odoo, filtra por escuela (ilike si viene) y hace fallback a reglas genéricas.
    Parsea patrones por líneas y comas. Cachea por 5 min.
    """
    now = time.time()
    key = school or "_all"
    if key in _INTENT_CACHE and now - _INTENT_CACHE[key]["ts"] < _INTENT_CACHE_TTL:
        return _INTENT_CACHE[key]["rules"]

    # --- Auth Odoo ---
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        return []

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    # --- Dominio por escuela (case-insensitive); sin 'active' para evitar errores si no existe el campo
    domain = []
    if school:
        domain.append(["x_studio_school", "ilike", school])

    rows = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "x_chatbot_intents", "search_read",
        [domain],
        {"fields": ["x_studio_category", "x_studio_patterns", "x_studio_priority"], "limit": 1000}
    )

    # Fallback a reglas genéricas (sin escuela) si no encontró por escuela
    if not rows:
        rows = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "x_chatbot_intents", "search_read",
            [[["x_studio_school", "=", False]]],
            {"fields": ["x_studio_category", "x_studio_patterns", "x_studio_priority"], "limit": 1000}
        )

    # --- Normalización de reglas
    rules = []
    for r in rows:
        raw = (r.get("x_studio_patterns") or "").strip()
        # Soporta líneas y/o comas como separadores
        pats = []
        for line in raw.splitlines():
            pats.extend([p.strip() for p in line.split(",") if p.strip()])

        rules.append({
            "cat":  r.get("x_studio_category"),
            "pats": [_norm(p) for p in pats],
            "prio": r.get("x_studio_priority") or 0,
        })

    rules.sort(key=lambda z: z["prio"], reverse=True)
    _INTENT_CACHE[key] = {"ts": now, "rules": rules}
    return rules


@app.post("/nlp/route")
async def nlp_route(request: Request):
    """
    Versión corregida: usa _load_rules (maneja 'ilike' por escuela y fallback),
    normaliza texto y patrones, y matchea por substring respetando prioridad.
    """
    try:
        data = await request.json()
        text = (data.get("text") or "").strip()
        school = (data.get("school") or "").strip()

        if not text:
            return {"found": False, "intent": None, "msg": "❌ No se recibió texto para analizar."}

        clean_text = _norm(text)
        rules = _load_rules(school)

        if not rules:
            return {"found": False, "intent": None, "msg": "⚠️ No hay reglas configuradas en Odoo."}

        for r in rules:  # ya vienen ordenadas por prioridad desc
            for pat in r["pats"]:
                if pat and pat in clean_text:
                    return {
                        "found": True,
                        "intent": r["cat"],
                        "matched_word": pat,
                        "school": school or None
                    }

        return {"found": False, "intent": None, "msg": "No se encontró intención."}

    except Exception as e:
        logger.exception("Error en /nlp/route")
        return {"found": False, "intent": None, "msg": f"Error procesando NLP: {str(e)}"}
        

MANYCHAT_API_KEY = "2663902:a54d0232e6fc431174e20594d5679c93"

headers = {
    "Authorization": f"Bearer {MANYCHAT_API_KEY}",
    "Content-Type": "application/json"
}

HEADERS = {
    "Authorization": f"Bearer {MANYCHAT_API_KEY}",
    "Content-Type": "application/json"
}

@app.post("/send_whatsapp")
async def send_whatsapp(request: Request):
    try:
        data = await request.json()

        phone = data.get("phone")
        name = data.get("name")
        order_number = data.get("order_number")
        status = data.get("status")

        # 1. Crear contacto
        create_url = "https://api.manychat.com/fb/subscriber/createSubscriber"

        create_payload = {
            "whatsapp_phone": phone,
            "first_name": name
        }

        create_response = requests.post(create_url, json=create_payload, headers=headers)

        subscriber_id = None

        try:
            create_json = create_response.json()
            subscriber_id = create_json.get("data", {}).get("id")
        except:
            pass

        # 2. Si no existe, buscarlo
        if not subscriber_id:
            find_url = "https://api.manychat.com/fb/subscriber/findBySystemField"

            find_payload = {
                "field_name": "whatsapp_phone",
                "field_value": phone
            }

            find_response = requests.post(find_url, json=find_payload, headers=headers)
            find_json = find_response.json()

            subscriber_id = find_json.get("data", {}).get("id")

        # 🔥 VALIDACIÓN CLAVE
        if not subscriber_id:
            return {
                "error": "No se pudo encontrar ni crear el contacto",
                "debug": {
                    "create": create_response.text,
                    "find": find_json
                }
            }

        # 3. Guardar campos
        set_field_url = "https://api.manychat.com/fb/subscriber/setCustomFieldByName"

        requests.post(set_field_url, json={
            "subscriber_id": subscriber_id,
            "field_name": "odoo_order_number",
            "field_value": order_number
        }, headers=headers)

        requests.post(set_field_url, json={
            "subscriber_id": subscriber_id,
            "field_name": "odoo_status_sporthouse",
            "field_value": status
        }, headers=headers)

        return {
            "subscriber_id": subscriber_id,
            "status": "ok"
        }

    except Exception as e:
        return {"error": str(e)}

@app.post("/update_whatsapp_fields")
async def update_whatsapp_fields(request: Request):
    try:
        data = await request.json()

        subscriber_id = data.get("subscriber_id")
        order_number = data.get("order_number")
        status = data.get("status")

        if not subscriber_id:
            return {"error": "Falta subscriber_id"}

        set_field_url = "https://api.manychat.com/fb/subscriber/setCustomFieldByName"

        r1 = requests.post(
            set_field_url,
            json={
                "subscriber_id": subscriber_id,
                "field_name": "odoo_order_number",
                "field_value": order_number
            },
            headers=HEADERS
        )

        r2 = requests.post(
            set_field_url,
            json={
                "subscriber_id": subscriber_id,
                "field_name": "odoo_status_sporthouse",
                "field_value": status
            },
            headers=HEADERS
        )

        return {
            "order_update_status": r1.status_code,
            "order_update_response": r1.text,
            "status_update_status": r2.status_code,
            "status_update_response": r2.text
        }

    except Exception as e:
        return {"error": str(e)}


# ============================================================
# ETIQUETAR CONTACTOS DE MONTEVERDE EN MANYCHAT
# ============================================================

MONTEVERDE_TAG = "Colegio - Monteverde"


def normalize_mexican_phone(phone):
    """
    Convierte el teléfono de Odoo a posibles formatos
    utilizados por WhatsApp y ManyChat.
    """
    digits = re.sub(r"\D", "", phone or "")

    if not digits:
        return []

    candidates = []

    # Ejemplo: 5512345678
    if len(digits) == 10:
        candidates.append(f"+52{digits}")
        candidates.append(f"+521{digits}")

    # Ejemplo: 525512345678
    elif len(digits) == 12 and digits.startswith("52"):
        candidates.append(f"+{digits}")
        candidates.append(f"+521{digits[-10:]}")

    # Ejemplo: 5215512345678
    elif len(digits) == 13 and digits.startswith("521"):
        candidates.append(f"+{digits}")
        candidates.append(f"+52{digits[-10:]}")

    else:
        candidates.append(f"+{digits}")

    return list(dict.fromkeys(candidates))


def find_manychat_contact(phone):
    """
    Busca un contacto existente en ManyChat.
    No crea contactos nuevos.
    """
    url = (
        "https://api.manychat.com/"
        "fb/subscriber/findBySystemField"
    )

    attempted_phones = []

    for formatted_phone in normalize_mexican_phone(phone):
        attempted_phones.append(formatted_phone)

        response = requests.post(
            url,
            json={
                "field_name": "whatsapp_phone",
                "field_value": formatted_phone
            },
            headers=HEADERS,
            timeout=20
        )

        try:
            response_data = response.json()
        except Exception:
            continue

        subscriber_id = (
            response_data
            .get("data", {})
            .get("id")
        )

        if subscriber_id:
            return {
                "subscriber_id": subscriber_id,
                "matched_phone": formatted_phone,
                "attempted_phones": attempted_phones
            }

    return {
        "subscriber_id": None,
        "matched_phone": None,
        "attempted_phones": attempted_phones
    }


def add_manychat_tag(subscriber_id):
    """
    Agrega la etiqueta Colegio - Monteverde.
    """
    url = (
        "https://api.manychat.com/"
        "fb/subscriber/addTagByName"
    )

    response = requests.post(
        url,
        json={
            "subscriber_id": int(subscriber_id),
            "tag_name": MONTEVERDE_TAG
        },
        headers=HEADERS,
        timeout=20
    )

    try:
        response_data = response.json()
    except Exception:
        response_data = {
            "raw_response": response.text
        }

    if not response.ok:
        raise Exception(
            f"Error ManyChat {response.status_code}: "
            f"{response.text}"
        )

    return response_data


@app.post("/tag_monteverde")
async def tag_monteverde(request: Request):
    """
    Busca los clientes con pedidos del sitio Monteverde
    y les agrega una etiqueta en ManyChat.

    JSON opcional:
    {
        "order_id": 123
    }

    Si no se manda order_id, revisa todos los pedidos históricos.
    """
    try:
        try:
            data = await request.json()
        except Exception:
            data = {}

        order_id = data.get("order_id") or data.get("id")

        # 1. Conectarse a Odoo
        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/common"
        )

        uid = common.authenticate(
            ODOO_DB,
            ODOO_USER,
            ODOO_PASSWORD,
            {}
        )

        if not uid:
            return {
                "status": "error",
                "message": "No se pudo autenticar con Odoo."
            }

        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/object"
        )

        # ----------------------------------------------------
        # Si se recibió un pedido específico
        # ----------------------------------------------------
        if order_id:
            try:
                order_id = int(order_id)
            except Exception:
                return {
                    "status": "error",
                    "message": "El order_id no es válido."
                }

            orders = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "sale.order",
                "search_read",
                [[
                    ["id", "=", order_id],
                    ["state", "!=", "cancel"]
                ]],
                {
                    "fields": [
                        "id",
                        "name",
                        "partner_id",
                        "website_id"
                    ],
                    "limit": 1
                }
            )

            if not orders:
                return {
                    "status": "not_found",
                    "message": "No se encontró el pedido."
                }

            website = orders[0].get("website_id")
            website_name = website[1] if website else ""

            if "monteverde" not in _norm(website_name):
                return {
                    "status": "skipped",
                    "message": "El pedido no pertenece a Monteverde.",
                    "order": orders[0].get("name"),
                    "website": website_name
                }

        # ----------------------------------------------------
        # Si no se recibió pedido, buscar todos los históricos
        # ----------------------------------------------------
        else:
            websites = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "website",
                "search_read",
                [[
                    ["name", "ilike", "Monteverde"]
                ]],
                {
                    "fields": ["id", "name"],
                    "limit": 20
                }
            )

            if not websites:
                return {
                    "status": "not_found",
                    "message": (
                        "No se encontró un sitio web de Odoo "
                        "con la palabra Monteverde."
                    )
                }

            website_ids = [
                website["id"]
                for website in websites
            ]

            orders = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "sale.order",
                "search_read",
                [[
                    ["website_id", "in", website_ids],
                    ["state", "!=", "cancel"]
                ]],
                {
                    "fields": [
                        "id",
                        "name",
                        "partner_id",
                        "website_id"
                    ],
                    "limit": 5000,
                    "order": "id desc"
                }
            )

        if not orders:
            return {
                "status": "completed",
                "orders_found": 0,
                "unique_clients": 0,
                "tagged": 0
            }

        # 2. Sacar clientes sin repetirlos
        partner_ids = list({
            order["partner_id"][0]
            for order in orders
            if order.get("partner_id")
        })

        if not partner_ids:
            return {
                "status": "completed",
                "orders_found": len(orders),
                "unique_clients": 0,
                "tagged": 0
            }

        # 3. Consultar datos de los clientes
        partners = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "res.partner",
            "search_read",
            [[
                ["id", "in", partner_ids]
            ]],
            {
                "fields": [
                    "id",
                    "name",
                    "phone",
                    "email"
                ],
                "limit": len(partner_ids)
            }
        )

        tagged_contacts = []
        without_phone = []
        not_found_in_manychat = []
        errors = []

        processed_subscribers = set()

        # 4. Buscar cada teléfono en ManyChat
        for partner in partners:
            phone = partner.get("phone")

            if not phone:
                without_phone.append({
                    "odoo_partner_id": partner.get("id"),
                    "name": partner.get("name")
                })
                continue

            try:
                manychat_contact = find_manychat_contact(
                    phone
                )

                subscriber_id = manychat_contact.get(
                    "subscriber_id"
                )

                if not subscriber_id:
                    not_found_in_manychat.append({
                        "odoo_partner_id": partner.get("id"),
                        "name": partner.get("name"),
                        "odoo_phone": phone,
                        "attempted_phones": (
                            manychat_contact.get(
                                "attempted_phones"
                            )
                        )
                    })
                    continue

                if subscriber_id in processed_subscribers:
                    continue

                # 5. Poner etiqueta
                add_manychat_tag(subscriber_id)

                processed_subscribers.add(subscriber_id)

                tagged_contacts.append({
                    "odoo_partner_id": partner.get("id"),
                    "name": partner.get("name"),
                    "odoo_phone": phone,
                    "manychat_phone": (
                        manychat_contact.get(
                            "matched_phone"
                        )
                    ),
                    "subscriber_id": subscriber_id,
                    "tag": MONTEVERDE_TAG
                })

                time.sleep(0.12)

            except Exception as error:
                errors.append({
                    "odoo_partner_id": partner.get("id"),
                    "name": partner.get("name"),
                    "phone": phone,
                    "error": str(error)
                })

        return {
            "status": "completed",
            "orders_found": len(orders),
            "unique_clients": len(partner_ids),
            "tagged": len(tagged_contacts),
            "without_phone_count": len(without_phone),
            "not_found_in_manychat_count": len(
                not_found_in_manychat
            ),
            "error_count": len(errors),
            "tagged_contacts": tagged_contacts,
            "without_phone": without_phone,
            "not_found_in_manychat": (
                not_found_in_manychat
            ),
            "errors": errors
        }

    except Exception as error:
        logger.exception(
            "Error en /tag_monteverde"
        )

        return {
            "status": "error",
            "message": str(error)
        }

