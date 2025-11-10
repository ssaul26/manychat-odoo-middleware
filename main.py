from fastapi import FastAPI, Query, Request
import xmlrpc.client
import os
from collections import defaultdict, OrderedDict
from datetime import datetime


app = FastAPI()

# 🔐 Variables de entorno
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")


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
                f"📦 {stock_label}",               # <<-- aquí el cambio de presentación
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
    Devuelve FAQs con formato limpio para ManyChat:
    - Títulos en negritas con emoji 📘
    - Preguntas con 💬
    - Respuestas separadas con saltos de línea
    - Sin uso de BeautifulSoup (solo regex)
    """
    import re, html, xmlrpc.client

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
            text = re.sub(r"</p\s*>", "\n\n", text)     # cierre de <p> → doble salto
            text = re.sub(r"<[^>]+>", "", text)         # elimina etiquetas restantes
            text = text.replace("\xa0", " ")
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text.strip()

        bloques = []
        for rec in faq_records:
            name = rec.get("name", "Preguntas Frecuentes")
            body = rec.get("body", "")

            texto = clean_html(body)

            # --- Formato visual mejorado ---
            # Detecta preguntas (terminan con ?)
            texto = re.sub(r"([^\n]*\?)", r"\n💬 *\1*\n", texto)
            texto = re.sub(r"\n{3,}", "\n\n", texto)  # compactar saltos


            bloque = f"\n📘 *{name}*\n{texto}\n\n"
            bloques.append(bloque)
        
        faq_msg = "\n".join(bloques).strip()
        return {"faq_msg": faq_msg, "total": len(bloques)}

    except Exception as e:
        return {"faq_msg": f"⚠️ Error al procesar las FAQ: {str(e)}"}

def normalize_datetime(s: str | None) -> str:
    if not s:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Intenta varios formatos (incluye el de ManyChat: '20 Oct 2025, 05:41pm')
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
    # Respaldo
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

from fastapi import FastAPI, Request
import xmlrpc.client, os
from datetime import datetime


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
        # Regla: buscar por messenger_id; si no hay, intenta por correo; si no por teléfono.
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
            # update (write)
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "x_interacciones_chatbo", "write",
                [existing_ids, vals]
            )
            rec_id = existing_ids[0]
            action = "updated"
        else:
            # create
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

        # 3) Mensaje formateado con emojis y saltos de línea
        mc_message = (
            f"👋 ¡Hola {client_name}!\n\n"
            f"📦 Tu pedido *{o['name']}* se realizó el 🗓️ {order_date} "
            f"por un total de 💰 *{order_total}*.\n\n"
            f"🚚 Si tienes dudas sobre tiempos o formas de entrega, "
            f"consulta nuestro apartado de *Preguntas Frecuentes* 📘.\n\n"
            f"Gracias por tu compra con *Sporthouse*! 💪"
        )

        return {
            "found": True,
            "client_name": client_name,
            "order_number": o["name"],
            "order_date": order_date,
            "order_total": order_total,
            "mc_message": mc_message
        }

    except Exception as e:
        return {"found": False, "mc_message": f"⚠️ Error al consultar pedido: {str(e)}"}


@app.post("/nlp/route")
async def nlp_route(request: Request):
    """
    Recibe:
    {
      "text": "quiero saber mi pedido",
      "school": "edron"
    }

    Devuelve:
    {
      "found": true,
      "intent": "seguimiento_pedido",
      "school": "edron",
      "matched_word": "pedido"
    }
    """
    try:
        data = await request.json()
        text = (data.get("text") or "").lower().strip()
        school = (data.get("school") or "").lower().strip()

        if not text:
            return {"found": False, "intent": None, "msg": "❌ No se recibió texto para analizar."}

        # --- Conexión a Odoo ---
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return {"found": False, "intent": None, "msg": "❌ Error autenticando en Odoo."}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # --- Buscar reglas activas de la escuela o genéricas ---
        domain = [["active", "=", True]]
        if school:
            domain.append(["x_school", "in", [school, False]])

        rules = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "x_chatbot_intent", "search_read",
            [domain],
            {"fields": ["x_school", "x_category", "x_patterns", "x_priority"], "limit": 200}
        )

        # --- Normalizar texto ---
        import unicodedata
        def normalize(t):
            return ''.join(
                c for c in unicodedata.normalize('NFD', t.lower())
                if unicodedata.category(c) != 'Mn'
            )

        clean_text = normalize(text)

        # --- Buscar coincidencia ---
        best_match = None
        for r in sorted(rules, key=lambda x: x.get("x_priority") or 0, reverse=True):
            patterns = (r.get("x_patterns") or "").splitlines()
            for p in patterns:
                if normalize(p.strip()) in clean_text:
                    best_match = {
                        "intent": r.get("x_category"),
                        "school": r.get("x_school"),
                        "matched_word": p.strip()
                    }
                    break
            if best_match:
                break

        if not best_match:
            return {"found": False, "intent": None, "msg": "No se encontró intención."}

        return {"found": True, **best_match}

    except Exception as e:
        return {"found": False, "intent": None, "msg": f"Error procesando NLP: {str(e)}"}
