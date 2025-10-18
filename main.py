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
    limit: int = Query(5, description="Número de productos a devolver"),
    category: str = Query(None, description="Filtrar por categoría (opcional)")
):
    try:
        # Conexión a Odoo
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})

        if not uid:
            return {"error": "❌ Error de autenticación con Odoo. Verifica tus credenciales."}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # --- Filtro por categoría opcional ---
        domain = [['active', '=', True]]
        if category:
            domain.append(['categ_id.name', 'ilike', category])

        # --- Leer productos (variantes) ---
        productos = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [domain],
            {
                'fields': [
                    'id', 'name', 'default_code', 'qty_available',
                    'list_price', 'product_tmpl_id', 'categ_id', 'barcode'
                ],
                'limit': limit
            }
        )

        # --- Normalizar productos ---
        def normalize(p):
            atributos = defaultdict(list)
            try:
                # Obtener valores de atributo de la plantilla asociada
                valores = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'product.template.attribute.value', 'search_read',
                    [[['product_tmpl_id', '=', p['product_tmpl_id'][0]]]],
                    {'fields': ['attribute_id', 'name']}
                )

                for v in valores:
                    if v.get('attribute_id'):
                        tipo = v['attribute_id'][1]  # Ej: "Color", "Talla"
                        atributos[tipo].append(v['name'])

            except Exception as e:
                print("Error leyendo atributos:", e)

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

        return {"productos": [normalize(p) for p in productos]}

    except Exception as e:
        return {"error": f"Ocurrió un error: {str(e)}"}
