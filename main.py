from fastapi import FastAPI, Query
import xmlrpc.client
import os
from collections import defaultdict

app = FastAPI()

# Variables de entorno (Railway)
# 🔐 Variables de entorno
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
@@ -17,7 +18,10 @@ def root():


@app.get("/inventario")
def get_inventario(limit: int = Query(5, description="Número de productos a devolver")):
def get_inventario(
    limit: int = Query(5, description="Número de productos a devolver"),
    category: str = Query(None, description="Filtrar por categoría (opcional)")
):
try:
# Conexión a Odoo
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
@@ -28,11 +32,16 @@ def get_inventario(limit: int = Query(5, description="Número de productos a dev

models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # --- Obtener variantes (product.product)
        # --- Filtro por categoría opcional ---
        domain = [['active', '=', True]]
        if category:
            domain.append(['categ_id.name', 'ilike', category])

        # --- Leer productos (variantes) ---
productos = models.execute_kw(
ODOO_DB, uid, ODOO_PASSWORD,
'product.product', 'search_read',
            [[['active', '=', True]]],
            [domain],
{
'fields': [
'id', 'name', 'default_code', 'qty_available',
@@ -42,20 +51,25 @@ def get_inventario(limit: int = Query(5, description="Número de productos a dev
}
)

        # --- Normalizar datos y agregar atributos de plantilla
        # --- Normalizar productos ---
def normalize(p):
            atributos = []
            atributos = defaultdict(list)
try:
                # Buscar valores de atributo desde product.template.attribute.value
                # Obtener valores de atributo de la plantilla asociada
valores = models.execute_kw(
ODOO_DB, uid, ODOO_PASSWORD,
'product.template.attribute.value', 'search_read',
[[['product_tmpl_id', '=', p['product_tmpl_id'][0]]]],
                    {'fields': ['name']}
                    {'fields': ['attribute_id', 'name']}
)
                atributos = [v['name'] for v in valores]
            except Exception:
                atributos = []

                for v in valores:
                    if v.get('attribute_id'):
                        tipo = v['attribute_id'][1]  # Ej: "Color", "Talla"
                        atributos[tipo].append(v['name'])

            except Exception as e:
                print("Error leyendo atributos:", e)

return {
"id": p["id"],
