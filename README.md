# ManyChat ↔ Odoo Middleware (FastAPI)

API que conecta ManyChat con Odoo ERP para consultar inventario por escuela (categoría de producto).

### Endpoints

- **GET /** → Estado del servicio.  
- **POST /consulta_inventario** → Envía un JSON como:
```json
{"escuela": "CEDROS"}
