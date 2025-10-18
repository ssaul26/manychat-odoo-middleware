from fastapi import FastAPI, Query
import xmlrpc.client
import os
from collections import defaultdict

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
    offset: int = Query(0, ge=0, description="Desplazamiento para paginar"),
    category: str | None = Query(None, description="Filtrar por categoría (opcional)"),
    format: str = Query("json", description="json | text (ManyChat)"),
):
    """
    Si format=json -> devuelve lista estructurada (como antes).
    Si format=text -> devuelve {"mensaje": "...", "next_offset": <int|None>, ...}
    para pegar directo en ManyChat (un único campo).
    """
    try:
        # Conexión a Odoo
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return {"error": "❌ Error de autenticación con Odoo. Verifica tus credenciales."}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # Dominio (filtros)
        domain = [['active', '=', True]]
        if category:
            # Filtra por nombre de categoría en la plantilla
            domain.append(['categ_id.name', 'ilike', category])

        # Total para saber si hay más páginas
        total_count = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_count',
            [domain]
        )

        # Leer productos (variantes) paginados
        productos = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [domain],
            {
                'fields': [
                    'id', 'name', 'default_code', 'qty_available',
                    'list_price', 'product_tmpl_id', 'categ_id', 'barcode'
                ],
                'limit': limit,
                'offset': offset,
                'order': 'id desc'
            }
        )

        # Helper: leer atributos por plantilla y agruparlos
        def obtener_atributos_por_template(product_tmpl_id: int):
            atributos = defaultdict(list)
            try:
                valores = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'product.template.attribute.value', 'search_read',
                    [[['product_tmpl_id', '=', product_tmpl_id]]],
                    {'fields': ['attribute_id', 'name']}
                )
                for v in valores:
                    if v.get('attribute_id'):
                        tipo = v['attribute_id'][1]  # "Color", "Talla", etc.
                        atributos[tipo].append(v['name'])
            except Exception:
                pass
            # Convierte defaultdict(list) a dict normal
            return {k: v for k, v in atributos.items()}

        # Normalización
        norm = []
        for p in productos:
            attrs = obtener_atributos_por_template(p['product_tmpl_id'][0]) if p.get('product_tmpl_id') else {}
            norm.append({
                "id": p["id"],
                "name": p["name"],
                "sku": p.get("default_code"),
                "price": p.get("list_price"),
                "stock": p.get("qty_available"),
                "template": p["product_tmpl_id"][1] if p.get("product_tmpl_id") else None,
                "category": p["categ_id"][1] if p.get("categ_id") else None,
                "attributes": attrs,
                "barcode": p.get("barcode")
            })

        # Salida JSON tradicional (por si la necesitas en otros flows)
        if format == "json":
            return {
                "total": total_count,
                "offset": offset,
                "limit": limit,
                "productos": norm
            }

        # Salida de TEXTO (pensada para ManyChat)
        # Encabezado
        titulo_cat = (category or "Sporthouse").upper()
        partes = [f"🌿 Catálogo para *{titulo_cat}* 🎒\n"]

        if not norm:
            partes.append("No encontramos productos para esta categoría. 😔")
        else:
            for p in norm:
                # Precio bonito (sin decimales si es entero)
                price = p["price"]
                price_txt = f"{int(price)}" if isinstance(price, (int, float)) and price == int(price) else f"{price:.2f}"

                partes.append(f"⭐ *{p['name']}*")
                if p.get("sku"):
                    partes.append(f"🆔 SKU: {p['sku']}")
                partes.append(f"💰 Precio: ${price_txt}")
                partes.append(f"📦 Stock: {int(p.get('stock') or 0)}")

                # Atributos formateados
                attrs = p.get("attributes") or {}
                for k, vals in attrs.items():
                    if vals:
                        partes.append(f"• {k}: {', '.join(vals)}")

                partes.append("")  # línea en blanco entre productos

        mensaje = "\n".join(partes).strip()

        # Paginación: ¿hay más?
        next_offset = offset + limit if (offset + limit) < total_count else None

        return {
            "mensaje": mensaje,
            "total": total_count,
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset,
        }

    except Exception as e:
        return {"error": f"Ocurrió un error: {str(e)}"}
