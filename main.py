from fastapi import FastAPI, Query
import xmlrpc.client
import os

app = FastAPI()

# Variables de entorno
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# Endpoint raíz para verificar que el API funcione
@app.get("/")
def root():
    return {"status": "✅ API funcionando correctamente 💫"}

# Ejemplo: obtener productos del inventario de Odoo
@app.get("/inventario")
def get_inventario(limit: int = Query(5, description="Número de productos a devolver")):
    try:
        # Conexión a Odoo
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})

        if not uid:
            return {"error": "❌ Error de autenticación con Odoo. Verifica tus credenciales."}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
# dentro de /inventario
productos = models.execute_kw(
    ODOO_DB, uid, ODOO_PASSWORD,
    'product.product', 'search_read',
    [[['active', '=', True]]],
    {
        'fields': [
            'id', 'name', 'default_code',   # SKU
            'qty_available', 'list_price',
            'product_tmpl_id',              # [id, nombre plantilla]
            'categ_id',                     # [id, nombre categoría] (viene desde la plantilla)
            'barcode'
        ],
        'limit': limit
    }
)

# Opcional: “limpiar” la salida para hacerla más legible
def normalize(p):
    return {
        "id": p["id"],
        "name": p["name"],
        "sku": p.get("default_code"),
        "price": p.get("list_price"),
        "stock": p.get("qty_available"),
        "template": p["product_tmpl_id"][1] if p.get("product_tmpl_id") else None,
        "category": p["categ_id"][1] if p.get("categ_id") else None,
        "barcode": p.get("barcode")
    }

return {"productos": [normalize(p) for p in productos]}


    except Exception as e:
        return {"error": f"Ocurrió un error: {str(e)}"}
