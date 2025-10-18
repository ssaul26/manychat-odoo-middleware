from fastapi import FastAPI, Query
import xmlrpc.client
import os

app = FastAPI()

# 🔐 Variables de entorno (Railway)
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")


# 🌐 Endpoint raíz: prueba de conexión
@app.get("/")
def root():
    return {"status": "✅ API funcionando correctamente 💫"}


# 📦 Endpoint: inventario con variantes y atributos
@app.get("/inventario")
def get_inventario(limit: int = Query(5, description="Número de productos a devolver")):
    try:
        # Conexión a Odoo
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})

        if not uid:
            return {"error": "❌ Error de autenticación con Odoo. Verifica tus credenciales."}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # 🔍 Leer productos activos con atributos
        productos = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[['active', '=', True]]],
            {
                'fields': [
                    'id', 'name', 'default_code', 'qty_available',
                    'list_price', 'product_tmpl_id', 'categ_id',
                    'barcode', 'attribute_value_ids'   # ← añadimos atributos
                ],
                'limit': limit
            }
        )

        # 🧩 Normalización de datos
        def normalize(p):
            atributos = []
            if p.get('attribute_value_ids'):
                try:
                    # Obtener nombres de los atributos por ID
                    atributos_raw = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        'product.attribute.value', 'read',
                        [p['attribute_value_ids']], {'fields': ['name']}
                    )
                    atributos = [a['name'] for a in atributos_raw]
                except Exception:
                    atributos = []

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

        # ✅ Respuesta final
        return {"productos": [normalize(p) for p in productos]}

    except Exception as e:
        return {"error": f"Ocurrió un error: {str(e)}"}
