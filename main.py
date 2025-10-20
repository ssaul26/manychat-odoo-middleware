from fastapi import FastAPI, Query
import xmlrpc.client
import os
from collections import defaultdict, OrderedDict
from bs4 import BeautifulSoup 

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
    Devuelve FAQs con formato legible para ManyChat:
    - Título del artículo en negritas
    - Preguntas en negritas con 💬
    - Listas con bullets
    - Saltos de línea correctos entre bloques
    """
    import re, html
    from bs4 import BeautifulSoup

    try:
        # Autenticación
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return {"faq_msg": "❌ Error de autenticación con Odoo."}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # Dominio (filtra por categoría si la envías)
        domain = []
        if category:
            domain.append(["name", "ilike", category])

        # Traer artículos
        faq_records = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "knowledge.article", "search_read",
            [domain],
            {"fields": ["name", "body"], "order": "name asc", "limit": 10}
        )

        if not faq_records:
            return {"faq_msg": f"⚠️ No se encontraron artículos para '{category}'."}

        # 🛠️ FUNCIÓN CORREGIDA
        def format_article(raw_html: str) -> str:
            soup = BeautifulSoup(raw_html or "", "html.parser")
            chunks = []

            # Recorremos bloques que suelen aparecer en Knowledge
            for el in soup.find_all(["h2", "h3", "p", "ul", "ol", "li", "br"]):
                # Texto del nodo
                txt = el.get_text(" ", strip=True)
                
                # Manejo de casos especiales para listas y vacíos
                if el.name in ("li",) and not txt: 
                    continue
                if not txt and el.name != "br":
                    continue
                
                # Nota: Li's serán manejados por su padre ul/ol. 
                # Si el.name == "li", ya lo procesaremos en ul/ol, 
                # pero nos aseguramos de no procesar los li's por separado si ya tienen texto.
                if el.name == "li" and el.parent and el.parent.name in ("ul", "ol"):
                    continue 

                if el.name in ("h2", "h3"):
                    # Separación fuerte para títulos de sección
                    chunks.append(f"\n\n📘 *{txt.upper()}*")

                elif el.name == "p":
                    # Si termina con ?, lo tratamos como “pregunta”
                    if txt.endswith("?"):
                        # Asegurar un salto de línea ANTES de la pregunta
                        chunks.append(f"\n💬 *{txt}*")
                    else:
                        # Para párrafos de respuesta, asegurar un salto DESPUÉS de la pregunta
                        # o una separación de un párrafo anterior
                        # El primer párrafo tendrá un salto, los siguientes dos saltos.
                        chunks.append(f"\n{txt}")

                elif el.name in ("ul", "ol"):
                    items = [f"• {li.get_text(' ', strip=True)}"
                             for li in el.find_all("li") if li.get_text(" ", strip=True)]
                    if items:
                        # Asegurar un salto de línea antes de la lista
                        chunks.append("\n" + "\n".join(items))

                elif el.name == "br":
                    chunks.append("\n") # salto de línea explícito

            # Unimos y limpiamos saltos múltiples
            # Unir con string vacío para tener control total de los \n
            text = "".join(chunks)

            text = html.unescape(text).replace("\xa0", " ")
            # Limpiamos 3 o más saltos seguidos a solo 2 saltos.
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            return text
        # 🛠️ FIN FUNCIÓN CORREGIDA

        bloques = []
        for rec in faq_records:
            name = rec.get("name", "Preguntas Frecuentes")
            body = rec.get("body", "")

            contenido = format_article(body)
            # Aseguramos dos saltos de línea tras el nombre del artículo
            bloque = f"📘 *{name}*\n\n{contenido}\n────────────────"
            bloques.append(bloque)

        faq_msg = "\n\n".join(bloques).strip()
        return {"faq_msg": faq_msg, "total": len(bloques)}

    except Exception as e:
        return {"faq_msg": f"⚠️ Error al procesar las FAQ: {str(e)}"}
