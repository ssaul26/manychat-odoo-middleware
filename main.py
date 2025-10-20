from fastapi import FastAPI, Query
import xmlrpc.client
import os
from collections import defaultdict, OrderedDict

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
def get_faq(
    format: str = Query("text", regex="^(json|text)$", description="json (default) o text")
):
    """
    Devuelve todas las preguntas frecuentes del módulo Knowledge (sin filtrar por escuela).
    """
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return {"error": "❌ Error de autenticación con Odoo."}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # Leer los artículos activos de Knowledge
        articulos = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'knowledge.article', 'search_read',
            [[['active', '=', True]]],
            {'fields': ['name', 'body'], 'limit': 10, 'order': 'create_date asc'}
        )

        if not articulos:
            return {"faq_msg": "No se encontraron preguntas frecuentes.", "total": 0}

        # Construir texto para ManyChat
        bloques = []
        for art in articulos:
            nombre = art.get('name', 'Artículo sin nombre')
            cuerpo = art.get('body') or ''
            if not isinstance(cuerpo, str):
                cuerpo = str(cuerpo)
            cuerpo = cuerpo.replace("<p>", "").replace("</p>", "")

            bloques.append(f"⭐ *{nombre}*\n\n{cuerpo}")

        msg = "\n\n".join(bloques)

        if format == "json":
            return {"articulos": articulos, "total": len(articulos)}
        else:
            return {"faq_msg": msg, "total": len(articulos)}

    except Exception as e:
        return {"error": f"⚠️ Ocurrió un error: {str(e)}"}

