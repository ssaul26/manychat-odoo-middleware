from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import Response
import xmlrpc.client, base64, os
from collections import defaultdict, OrderedDict

app = FastAPI()

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")


@app.get("/")
def root():
    return {"status": "✅ API funcionando correctamente 💫"}


# 🔹 Endpoint nuevo: para devolver la imagen del producto
@app.get("/image/template/{template_id}")
def get_template_image(template_id: int):
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(status_code=401, detail="Auth Odoo failed")

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        rec = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.template', 'read',
            [[template_id]],
            {'fields': ['image_1920']}
        )
        if not rec or not rec[0].get('image_1920'):
            raise HTTPException(status_code=404, detail="No image")

        img_data = base64.b64decode(rec[0]['image_1920'])
        return Response(content=img_data, media_type="image/png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 🔹 Catálogo de productos
@app.get("/inventario")
def get_inventario(
    limit: int = Query(5), offset: int = Query(0),
    category: str = Query(None), format: str = Query("json")
):
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            return {"catalogo_msg": "❌ Error de autenticación", "next_offset": 0}

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

        domain = [['active', '=', True]]
        if category:
            domain.append(['categ_id.complete_name', 'ilike', category])

        templates = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.template', 'search_read',
            [domain],
            {'fields': ['id', 'name', 'list_price', 'qty_available', 'categ_id', 'image_1920'],
             'limit': limit, 'offset': offset, 'order': 'name asc'}
        )

        def normalize(t):
            image_url = None
            if BASE_URL and t.get("image_1920"):
                image_url = f"{BASE_URL}/image/template/{t['id']}"
            stock = int(t.get("qty_available") or 0)
            stock_label = "DISPONIBLE" if stock > 0 else "NO DISPONIBLE"
            return {
                "id": t["id"],
                "name": t["name"],
                "price": t["list_price"],
                "stock": stock,
                "stock_label": stock_label,
                "category": t["categ_id"][1] if t.get("categ_id") else None,
                "image_url": image_url
            }

        items = [normalize(t) for t in templates]
        next_offset = (offset + limit) if len(items) == limit else 0

        if format == "json":
            return {"productos": items, "next_offset": next_offset}

        header = f"🌿 *Catálogo para {category or 'tu selección'}* 🌸\n"
        if not items:
            return {"catalogo_msg": header + "No hay productos.", "next_offset": 0}

        bloques = []
        for it in items:
            lineas = [
                f"⭐ *{it['name']}*",
                f"💰 Precio: ${it['price'] or 0}",
                f"📦 {it['stock_label']}"
            ]
            if it["image_url"]:
                lineas.append(f"🖼 Imagen: {it['image_url']}")
            bloques.append("\n".join(lineas))

        return {"catalogo_msg": header + "\n\n".join(bloques), "next_offset": next_offset}
    except Exception as e:
        return {"catalogo_msg": f"⚠️ Error: {str(e)}", "next_offset": 0}
