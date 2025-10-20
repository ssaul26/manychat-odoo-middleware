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
    Devuelve productos de Odoo a nivel product.template (1 por producto).
    - format=json  -> {"productos":[...], "next_offset": <int>}
    - format=text  -> {"catalogo_msg": "...", "next_offset": <int>}
    """

    try:
        # 1) Autenticación
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            # Para ManyChat conviene devolver catalogo_msg siempre que sea posible
            return {"catalogo_msg": "❌ Error de autenticación con Odoo. Verifica credenciales.", "next_offset": 0}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        # 2) Dominio de búsqueda (plantillas activas; opcionalmente en venta)
        domain = [['active', '=', True]]
        # Si quieres solo productos vendibles, descomenta:
        # domain.append(['sale_ok', '=', True])
        if category:
            domain.append(['categ_id.name', 'ilike', category])

        # 3) Leer PRODUCT TEMPLATES con paginación
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

        # 4) Normalización + atributos por plantilla
        def normalize_template(t):
            atributos = defaultdict(list)
            try:
                valores = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'product.template.attribute.value', 'search_read',
                    [[['product_tmpl_id', '=', t['id']]]],
                    {'fields': ['attribute_id', 'name']}
                )
                for v in valores:
                    if v.get('attribute_id'):
                        tipo = v['attribute_id'][1]  # p.ej. "Color", "Talla"
                        if v.get('name'):
                            atributos[tipo].append(v['name'])
            except Exception:
                pass

            return {
                "id": t["id"],                              # id de template
                "name": t.get("name"),
                "price": t.get("list_price"),
                "stock": t.get("qty_available"),            # suma de variantes
                "template": t.get("name"),
                "category": t["categ_id"][1] if t.get("categ_id") else None,
                "attributes": atributos,
                "sku": None,                                # SKU suele estar por variante
                "barcode": None                             # barcode suele estar por variante
            }

        items = [normalize_template(t) for t in templates]

        # 5) Paginación: si llenó la página, asumimos que hay más
        next_offset = (offset + limit) if len(items) == limit else 0

        # 6) Respuesta según formato
        if format == "json":
            return {"productos": items, "next_offset": next_offset}

        # ---- format == "text": construir catalogo_msg listo para ManyChat ----
        header = f"🌿 *Catálogo para {category or 'tu selección'}* 🌸\n"
        if not items:
            return {"catalogo_msg": header + "No encontramos productos por ahora. 🙈", "next_offset": 0}

        bloques = []
        for it in items:
            lineas = [
                f"⭐ *{it.get('name') or 'Producto'}*",
                f"💰 Precio: ${it.get('price') or 0}",
                f"📦 Stock: {int(it.get('stock') or 0)}",
            ]
            # Añade 1-2 atributos principales si existen (Color/Talla, etc.)
            attrs = it.get("attributes") or {}
            for k, vals in list(attrs.items())[:2]:
                if vals:
                    lineas.append(f"• {k}: {', '.join(vals[:5])}")
            bloques.append("\n".join(lineas))

        catalogo_msg = header + "\n" + "\n\n".join(bloques)
        return {"catalogo_msg": catalogo_msg, "next_offset": next_offset}

    except Exception as e:
        # Devolver siempre algo que ManyChat pueda mostrar
        return {"catalogo_msg": f"⚠️ Hubo un error obteniendo el catálogo.\n\nDetalle: {str(e)}", "next_offset": 0}
