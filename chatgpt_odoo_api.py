"""
Sub-API de solo lectura para conectar un GPT personalizado con Odoo.

Uso en tu main.py:

    from chatgpt_odoo_api import chatgpt_app
    app.mount("/chatgpt", chatgpt_app)

Variables de entorno requeridas en Railway:
    ODOO_URL
    ODOO_DB
    ODOO_USER
    ODOO_PASSWORD
    CHATGPT_ACTION_KEY

Variable opcional:
    PUBLIC_BASE_URL=https://manychat-odoo-middleware-production.up.railway.app
"""

from __future__ import annotations

import logging
import os
import secrets
import xmlrpc.client
from collections import defaultdict
from datetime import date
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("chatgpt_odoo_api")

ODOO_URL = (os.getenv("ODOO_URL") or "").rstrip("/")
ODOO_DB = os.getenv("ODOO_DB") or ""
ODOO_USER = os.getenv("ODOO_USER") or ""
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD") or ""
CHATGPT_ACTION_KEY = os.getenv("CHATGPT_ACTION_KEY") or ""
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or "https://manychat-odoo-middleware-production.up.railway.app"
).rstrip("/")

bearer_scheme = HTTPBearer(auto_error=False)
_FIELD_CACHE: dict[str, set[str]] = {}


def require_chatgpt_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    """Valida la API key enviada por el GPT como Authorization: Bearer <key>."""
    if not CHATGPT_ACTION_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CHATGPT_ACTION_KEY no está configurada en Railway.",
        )

    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(credentials.credentials, CHATGPT_ACTION_KEY)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credencial inválida.",
            headers={"WWW-Authenticate": "Bearer"},
        )


chatgpt_app = FastAPI(
    title="Odoo Sporthouse - ChatGPT API",
    description=(
        "API de solo lectura para consultar pedidos, inventario, clientes "
        "y entregas de Odoo desde un GPT personalizado."
    ),
    version="1.0.0",
    servers=[{"url": f"{PUBLIC_BASE_URL}/chatgpt"}],
    dependencies=[Depends(require_chatgpt_key)],
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)


def _validate_odoo_config() -> None:
    missing = [
        name
        for name, value in {
            "ODOO_URL": ODOO_URL,
            "ODOO_DB": ODOO_DB,
            "ODOO_USER": ODOO_USER,
            "ODOO_PASSWORD": ODOO_PASSWORD,
        }.items()
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Faltan variables de Odoo: {', '.join(missing)}.",
        )


def _odoo_connection() -> tuple[int, xmlrpc.client.ServerProxy]:
    _validate_odoo_config()
    try:
        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/common", allow_none=True
        )
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not uid:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Odoo rechazó la autenticación.",
            )
        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/object", allow_none=True
        )
        return int(uid), models
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("No fue posible conectar con Odoo")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No fue posible conectar con Odoo.",
        ) from exc


def _execute(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    model: str,
    method: str,
    args: list[Any],
    kwargs: dict[str, Any] | None = None,
) -> Any:
    try:
        return models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            model,
            method,
            args,
            kwargs or {},
        )
    except Exception as exc:
        logger.exception("Error Odoo en %s.%s", model, method)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Odoo no pudo completar la consulta de {model}.",
        ) from exc


def _model_fields(
    models: xmlrpc.client.ServerProxy, uid: int, model: str
) -> set[str]:
    if model not in _FIELD_CACHE:
        metadata = _execute(
            models,
            uid,
            model,
            "fields_get",
            [],
            {"attributes": ["type"]},
        )
        _FIELD_CACHE[model] = set(metadata.keys())
    return _FIELD_CACHE[model]


def _safe_search_read(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    model: str,
    domain: list[Any],
    wanted_fields: list[str],
    *,
    limit: int = 50,
    offset: int = 0,
    order: str = "id desc",
) -> list[dict[str, Any]]:
    available = _model_fields(models, uid, model)
    fields = [field for field in wanted_fields if field in available]
    return _execute(
        models,
        uid,
        model,
        "search_read",
        [domain],
        {
            "fields": fields,
            "limit": limit,
            "offset": offset,
            "order": order,
        },
    )


def _many2one(value: Any) -> dict[str, Any] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return {"id": value[0], "name": value[1]}
    return None


def _or_domain(clauses: list[list[Any]]) -> list[Any]:
    if not clauses:
        return []
    if len(clauses) == 1:
        return clauses
    return (["|"] * (len(clauses) - 1)) + clauses


def _state_label(state_value: str | None) -> str:
    labels = {
        "draft": "Borrador",
        "waiting": "Esperando otra operación",
        "confirmed": "Esperando disponibilidad",
        "assigned": "Listo",
        "done": "Hecho / entregado",
        "cancel": "Cancelado",
    }
    return labels.get(state_value or "", state_value or "Sin estado")


def _normalize_order(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "numero": row.get("name"),
        "cliente": _many2one(row.get("partner_id")),
        "fecha": row.get("date_order") or None,
        "estado": row.get("state") or None,
        "total": row.get("amount_total") or 0,
        "moneda": _many2one(row.get("currency_id")),
        "escuela_sitio": _many2one(row.get("website_id")),
        "estado_facturacion": row.get("invoice_status") or None,
    }


def _normalize_picking(row: dict[str, Any]) -> dict[str, Any]:
    custom_status = row.get("x_studio_estado_sporthouse")
    return {
        "id": row.get("id"),
        "numero_entrega": row.get("name"),
        "pedido": row.get("origin") or None,
        "cliente": _many2one(row.get("partner_id")),
        "estado_odoo": row.get("state") or None,
        "estado_odoo_texto": _state_label(row.get("state")),
        "estado_sporthouse": custom_status or None,
        "fecha_programada": row.get("scheduled_date") or None,
        "fecha_realizada": row.get("date_done") or None,
        "tipo_operacion": _many2one(row.get("picking_type_id")),
    }


@chatgpt_app.get(
    "/health",
    operation_id="verificarConexionOdoo",
    summary="Verificar conexión con Odoo",
    openapi_extra={"x-openai-isConsequential": False},
)
def health() -> dict[str, Any]:
    uid, _ = _odoo_connection()
    return {"status": "ok", "odoo_authenticated": True, "uid": uid}


@chatgpt_app.get(
    "/pedidos",
    operation_id="consultarPedidos",
    summary="Consultar una lista de pedidos",
    description=(
        "Busca pedidos por número, cliente, escuela/sitio, fecha o estado. "
        "Usa el estado técnico de Odoo: draft, sent, sale o cancel."
    ),
    openapi_extra={"x-openai-isConsequential": False},
)
def consultar_pedidos(
    numero: str | None = Query(None, description="Número completo o parcial, por ejemplo S00413"),
    cliente: str | None = Query(None, description="Nombre parcial del cliente"),
    escuela: str | None = Query(None, description="Nombre parcial del sitio o escuela"),
    estado: str | None = Query(None, description="Estado técnico: draft, sent, sale o cancel"),
    fecha_desde: date | None = Query(None, description="Fecha inicial YYYY-MM-DD"),
    fecha_hasta: date | None = Query(None, description="Fecha final YYYY-MM-DD"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    uid, models = _odoo_connection()
    domain: list[Any] = []

    if numero:
        domain.append(["name", "ilike", numero.strip()])
    if cliente:
        domain.append(["partner_id.name", "ilike", cliente.strip()])
    if escuela:
        domain.append(["website_id.name", "ilike", escuela.strip()])
    if estado:
        domain.append(["state", "=", estado.strip().lower()])
    if fecha_desde:
        domain.append(["date_order", ">=", f"{fecha_desde.isoformat()} 00:00:00"])
    if fecha_hasta:
        domain.append(["date_order", "<=", f"{fecha_hasta.isoformat()} 23:59:59"])

    rows = _safe_search_read(
        models,
        uid,
        "sale.order",
        domain,
        [
            "id",
            "name",
            "partner_id",
            "date_order",
            "state",
            "amount_total",
            "currency_id",
            "website_id",
            "invoice_status",
        ],
        limit=limit,
        offset=offset,
        order="date_order desc, id desc",
    )

    orders = [_normalize_order(row) for row in rows]
    order_names = [item["numero"] for item in orders if item.get("numero")]

    deliveries_by_origin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if order_names:
        picking_rows = _safe_search_read(
            models,
            uid,
            "stock.picking",
            [["origin", "in", order_names]],
            [
                "id",
                "name",
                "origin",
                "partner_id",
                "state",
                "scheduled_date",
                "date_done",
                "picking_type_id",
                "x_studio_estado_sporthouse",
            ],
            limit=min(500, max(100, len(order_names) * 5)),
            order="id desc",
        )
        for picking in picking_rows:
            deliveries_by_origin[picking.get("origin") or ""].append(
                _normalize_picking(picking)
            )

    for order in orders:
        order["entregas"] = deliveries_by_origin.get(order.get("numero") or "", [])

    return {
        "count": len(orders),
        "offset": offset,
        "limit": limit,
        "pedidos": orders,
    }


@chatgpt_app.get(
    "/pedidos/{numero}",
    operation_id="consultarDetallePedido",
    summary="Consultar el detalle de un pedido",
    openapi_extra={"x-openai-isConsequential": False},
)
def consultar_detalle_pedido(numero: str) -> dict[str, Any]:
    uid, models = _odoo_connection()
    rows = _safe_search_read(
        models,
        uid,
        "sale.order",
        [["name", "=ilike", numero.strip()]],
        [
            "id",
            "name",
            "partner_id",
            "date_order",
            "state",
            "amount_total",
            "amount_untaxed",
            "amount_tax",
            "currency_id",
            "website_id",
            "invoice_status",
            "client_order_ref",
            "note",
        ],
        limit=1,
        order="id desc",
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"No se encontró el pedido {numero}.")

    raw_order = rows[0]
    result = _normalize_order(raw_order)
    result.update(
        {
            "subtotal": raw_order.get("amount_untaxed") or 0,
            "impuestos": raw_order.get("amount_tax") or 0,
            "referencia_cliente": raw_order.get("client_order_ref") or None,
            "nota": raw_order.get("note") or None,
        }
    )

    line_rows = _safe_search_read(
        models,
        uid,
        "sale.order.line",
        [["order_id", "=", raw_order["id"]]],
        [
            "id",
            "product_id",
            "name",
            "product_uom_qty",
            "qty_delivered",
            "price_unit",
            "price_subtotal",
        ],
        limit=500,
        order="id asc",
    )
    result["productos"] = [
        {
            "id": line.get("id"),
            "producto": _many2one(line.get("product_id")),
            "descripcion": line.get("name") or None,
            "cantidad": line.get("product_uom_qty") or 0,
            "cantidad_entregada": line.get("qty_delivered") or 0,
            "precio_unitario": line.get("price_unit") or 0,
            "subtotal": line.get("price_subtotal") or 0,
        }
        for line in line_rows
    ]

    picking_rows = _safe_search_read(
        models,
        uid,
        "stock.picking",
        [["origin", "=", raw_order.get("name")]],
        [
            "id",
            "name",
            "origin",
            "partner_id",
            "state",
            "scheduled_date",
            "date_done",
            "picking_type_id",
            "x_studio_estado_sporthouse",
        ],
        limit=100,
        order="id desc",
    )
    result["entregas"] = [_normalize_picking(row) for row in picking_rows]
    return result


@chatgpt_app.get(
    "/inventario",
    operation_id="consultarInventario",
    summary="Consultar inventario por variante",
    description=(
        "Consulta productos y variantes por nombre, SKU, código de barras o categoría. "
        "El stock corresponde al total visible para el usuario API de Odoo."
    ),
    openapi_extra={"x-openai-isConsequential": False},
)
def consultar_inventario(
    buscar: str | None = Query(None, description="Nombre, SKU o código de barras"),
    categoria: str | None = Query(None, description="Categoría parcial"),
    solo_con_stock: bool = Query(False),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    uid, models = _odoo_connection()
    available = _model_fields(models, uid, "product.product")
    domain: list[Any] = []

    if "active" in available:
        domain.append(["active", "=", True])
    if buscar:
        term = buscar.strip()
        clauses = [
            [field, "ilike", term]
            for field in ("name", "default_code", "barcode")
            if field in available
        ]
        domain.extend(_or_domain(clauses))
    if categoria and "categ_id" in available:
        domain.append(["categ_id.name", "ilike", categoria.strip()])
    if solo_con_stock and "qty_available" in available:
        domain.append(["qty_available", ">", 0])

    rows = _safe_search_read(
        models,
        uid,
        "product.product",
        domain,
        [
            "id",
            "name",
            "display_name",
            "default_code",
            "barcode",
            "qty_available",
            "free_qty",
            "virtual_available",
            "incoming_qty",
            "outgoing_qty",
            "list_price",
            "categ_id",
            "product_tmpl_id",
            "uom_id",
            "sale_ok",
        ],
        limit=limit,
        offset=offset,
        order="name asc, id asc",
    )

    products = [
        {
            "id": row.get("id"),
            "nombre": row.get("display_name") or row.get("name"),
            "sku": row.get("default_code") or None,
            "codigo_barras": row.get("barcode") or None,
            "stock_a_mano": row.get("qty_available") or 0,
            "stock_libre": row.get("free_qty") if "free_qty" in row else None,
            "stock_pronosticado": (
                row.get("virtual_available") if "virtual_available" in row else None
            ),
            "entrante": row.get("incoming_qty") if "incoming_qty" in row else None,
            "saliente": row.get("outgoing_qty") if "outgoing_qty" in row else None,
            "precio": row.get("list_price") or 0,
            "categoria": _many2one(row.get("categ_id")),
            "plantilla": _many2one(row.get("product_tmpl_id")),
            "unidad": _many2one(row.get("uom_id")),
            "vendible": bool(row.get("sale_ok")),
        }
        for row in rows
    ]
    return {
        "count": len(products),
        "offset": offset,
        "limit": limit,
        "productos": products,
    }


@chatgpt_app.get(
    "/clientes",
    operation_id="consultarClientes",
    summary="Buscar clientes",
    description="Busca clientes por nombre, correo, teléfono, celular o referencia.",
    openapi_extra={"x-openai-isConsequential": False},
)
def consultar_clientes(
    buscar: str | None = Query(None, description="Nombre, correo, teléfono o referencia"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    uid, models = _odoo_connection()
    available = _model_fields(models, uid, "res.partner")
    domain: list[Any] = []

    if "active" in available:
        domain.append(["active", "=", True])
    if "customer_rank" in available:
        domain.append(["customer_rank", ">", 0])
    if buscar:
        term = buscar.strip()
        clauses = [
            [field, "ilike", term]
            for field in ("name", "email", "phone", "mobile", "ref")
            if field in available
        ]
        domain.extend(_or_domain(clauses))

    rows = _safe_search_read(
        models,
        uid,
        "res.partner",
        domain,
        [
            "id",
            "name",
            "email",
            "phone",
            "mobile",
            "ref",
            "city",
            "state_id",
            "country_id",
            "customer_rank",
        ],
        limit=limit,
        offset=offset,
        order="name asc, id asc",
    )

    clients = [
        {
            "id": row.get("id"),
            "nombre": row.get("name"),
            "correo": row.get("email") or None,
            "telefono": row.get("phone") or None,
            "celular": row.get("mobile") or None,
            "referencia": row.get("ref") or None,
            "ciudad": row.get("city") or None,
            "estado": _many2one(row.get("state_id")),
            "pais": _many2one(row.get("country_id")),
            "rango_cliente": row.get("customer_rank") or 0,
        }
        for row in rows
    ]
    return {
        "count": len(clients),
        "offset": offset,
        "limit": limit,
        "clientes": clients,
    }


@chatgpt_app.get(
    "/entregas",
    operation_id="consultarEntregas",
    summary="Consultar entregas",
    description=(
        "Busca transferencias/entregas por número, pedido, cliente, escuela, "
        "estado de Odoo o estado personalizado de Sporthouse."
    ),
    openapi_extra={"x-openai-isConsequential": False},
)
def consultar_entregas(
    numero: str | None = Query(None, description="Número de entrega, por ejemplo WH/OUT/00001"),
    pedido: str | None = Query(None, description="Número de pedido/origen"),
    cliente: str | None = Query(None, description="Nombre parcial del cliente"),
    escuela: str | None = Query(None, description="Escuela o sitio del pedido"),
    estado_odoo: str | None = Query(None, description="draft, waiting, confirmed, assigned, done o cancel"),
    estado_sporthouse: str | None = Query(None, description="Estado personalizado de Sporthouse"),
    fecha_desde: date | None = Query(None, description="Fecha programada inicial YYYY-MM-DD"),
    fecha_hasta: date | None = Query(None, description="Fecha programada final YYYY-MM-DD"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    uid, models = _odoo_connection()
    picking_fields = _model_fields(models, uid, "stock.picking")
    domain: list[Any] = []

    if numero:
        domain.append(["name", "ilike", numero.strip()])
    if pedido:
        domain.append(["origin", "ilike", pedido.strip()])
    if cliente:
        domain.append(["partner_id.name", "ilike", cliente.strip()])
    if estado_odoo:
        domain.append(["state", "=", estado_odoo.strip().lower()])
    if estado_sporthouse and "x_studio_estado_sporthouse" in picking_fields:
        domain.append([
            "x_studio_estado_sporthouse",
            "ilike",
            estado_sporthouse.strip(),
        ])
    if fecha_desde:
        domain.append(["scheduled_date", ">=", f"{fecha_desde.isoformat()} 00:00:00"])
    if fecha_hasta:
        domain.append(["scheduled_date", "<=", f"{fecha_hasta.isoformat()} 23:59:59"])

    if escuela:
        order_rows = _safe_search_read(
            models,
            uid,
            "sale.order",
            [["website_id.name", "ilike", escuela.strip()]],
            ["name"],
            limit=5000,
            order="id desc",
        )
        order_names = [row.get("name") for row in order_rows if row.get("name")]
        if not order_names:
            return {"count": 0, "offset": offset, "limit": limit, "entregas": []}
        domain.append(["origin", "in", order_names])

    rows = _safe_search_read(
        models,
        uid,
        "stock.picking",
        domain,
        [
            "id",
            "name",
            "origin",
            "partner_id",
            "state",
            "scheduled_date",
            "date_done",
            "picking_type_id",
            "x_studio_estado_sporthouse",
        ],
        limit=limit,
        offset=offset,
        order="scheduled_date desc, id desc",
    )
    deliveries = [_normalize_picking(row) for row in rows]
    return {
        "count": len(deliveries),
        "offset": offset,
        "limit": limit,
        "entregas": deliveries,
    }


@chatgpt_app.get(
    "/entregas/{numero}",
    operation_id="consultarDetalleEntrega",
    summary="Consultar detalle de una entrega",
    openapi_extra={"x-openai-isConsequential": False},
)
def consultar_detalle_entrega(numero: str) -> dict[str, Any]:
    uid, models = _odoo_connection()
    rows = _safe_search_read(
        models,
        uid,
        "stock.picking",
        [["name", "=ilike", numero.strip()]],
        [
            "id",
            "name",
            "origin",
            "partner_id",
            "state",
            "scheduled_date",
            "date_done",
            "picking_type_id",
            "x_studio_estado_sporthouse",
        ],
        limit=1,
        order="id desc",
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"No se encontró la entrega {numero}.")

    raw = rows[0]
    result = _normalize_picking(raw)
    move_rows = _safe_search_read(
        models,
        uid,
        "stock.move",
        [["picking_id", "=", raw["id"]]],
        [
            "id",
            "product_id",
            "product_uom_qty",
            "quantity",
            "product_uom",
            "state",
        ],
        limit=500,
        order="id asc",
    )
    result["productos"] = [
        {
            "id": move.get("id"),
            "producto": _many2one(move.get("product_id")),
            "cantidad_solicitada": move.get("product_uom_qty") or 0,
            "cantidad_procesada": move.get("quantity") if "quantity" in move else None,
            "unidad": _many2one(move.get("product_uom")),
            "estado": move.get("state") or None,
        }
        for move in move_rows
    ]
    return result
