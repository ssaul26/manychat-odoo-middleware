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
    offset: int = Query(0, ge=0, description="Desplazamiento (paginación)"),
    category: str = Query(None, description="Filtrar por categoría (opcional)"),
    format: str = Query("json", regex="^(json|text)$", description="json (default) o text")
):
    """
    Devuelve productos de Odoo.
    - format=json  -> {"productos":[...]}   (como ya lo usabas)
    - format=text  -> {"catalogo_msg": "...", "next_offset": <int>}
    """

    try:
        # 1) Autenticación
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return {"error": "❌ Error de autenticación con Odoo. Verifica credenciales."}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # 2) Dominio de búsqueda
        domain = [['active', '=', True]]
        if category:
            domain.append(['categ_id.name', 'ilike', category])

        # 3) Leer variantes con paginación
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
                'order': 'id asc',
            }
        )

        # 4) Normalización + atributos (opcional)
        def normalize(p):
            atributos = defaultdict(list)
            try:
                # Valores de atributo de la PLANTILLA asociada
                valores = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'product.template.attribute.value', 'search_read',
                    [[['product_tmpl_id', '=', p['product_tmpl_id'][0]]]],
                    {'fields': ['attribute_id', 'name']}
                )
                for v in valores:
                    if v.get('attribute_id'):
                        tipo = v['attribute_id'][1]  # "Color", "Talla", etc.
                        atributos[tipo].append(v['name'])
            except Exception:
                pass

            return {
                "id": p["id"],
                "name": p["name"],
                "sku": p.get("default_code"),
                "price": p.get("list_price"),
                "stock": p.get("qty_available"),
                "template": p["product_tmpl_id"][1] if p.get("product_tmpl_id") else None,
                "category": p["categ_id"][1] if p.get("categ_id") else None,
                "attributes": atributos,
                "barcode": p.get("barcode")
            }

        items = [normalize(p) for p in productos]

        # 5) Calcular next_offset para paginación
        #    Si trajo 'limit' elementos, asumimos que puede haber más.
        next_offset = (offset + limit) if len(items) == limit else 0

        # 6) Respuesta según formato
        if format == "json":
            return {"productos": items, "next_offset": next_offset}

        # ---- format == "text": construir catalogo_msg listo para ManyChat ----
        # Encabezado
        header = f"🌿 *Catálogo para {category or 'tu selección'}* 🌸\n"
        if not items:
            # Sin resultados
            body = "No encontramos productos por ahora. 🙈"
            return {"catalogo_msg": header + body, "next_offset": 0}

        # Cuerpo (una tarjeta por producto)
        bloques = []
        for it in items:
            lineas = [
                "⭐ *" + (it['name'] or 'Producto') + "*",
                f"💰 Precio: ${it['price'] or 0}",
                f"📦 Stock: {int(it['stock'] or 0)}",
            ]
            # Puedes añadir atributos principales si quieres
            # p.ej. Color/Talla si existen
            if it["attributes"]:
                # aplanamos atributos principales
                for k, vals in list(it["attributes"].items())[:2]:
                    if vals:
                        lineas.append(f"• {k}: {', '.join(vals[:5])}")
            bloques.append("\n".join(lineas))

        body = "\n\n".join(bloques)
        catalogo_msg = header + "\n" + body

        return {"catalogo_msg": catalogo_msg, "next_offset": next_offset}

    except Exception as e:
        return {"error": f"Ocurrió un error: {str(e)}"}
