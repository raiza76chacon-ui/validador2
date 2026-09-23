import os
import io
import psycopg2
import json
import re
import pypdf
from datetime import datetime
import streamlit as st
import pandas as pd
from google import genai
from google.genai import types
from openai import OpenAI
import time

# ==========================================
# CONFIGURACIÓN DE SECRETOS Y CONEXIÓN
# ==========================================
DATABASE_URL = os.environ.get("DATABASE_URL") or st.secrets.get("DATABASE_URL")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or st.secrets.get("GEMINI_API_KEY")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY") or st.secrets.get("OPENROUTER_API_KEY")
ADMIN_USER = os.environ.get("ADMIN_USER") or st.secrets.get("ADMIN_USER")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or st.secrets.get("ADMIN_PASSWORD")

client = genai.Client(api_key=GEMINI_KEY)
alt_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_KEY)

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS productos (
            id SERIAL PRIMARY KEY,
            nombre_display TEXT NOT NULL,
            nombre_normalizado TEXT UNIQUE NOT NULL,
            tienda TEXT NOT NULL,
            veces_exitoso INTEGER DEFAULT 0,
            veces_rechazado INTEGER DEFAULT 0,
            ultima_actualizacion TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historial_compras (
            id SERIAL PRIMARY KEY,
            producto_id INTEGER,
            estado TEXT,
            ip TEXT,
            ciudad TEXT,
            isp TEXT,
            tipo_conexion TEXT,
            sede TEXT,
            zip_code TEXT,
            fecha TIMESTAMP,
            FOREIGN KEY(producto_id) REFERENCES productos(id)
        )
    """)
    conn.commit()
    conn.close()

def normalizar_texto(texto: str) -> str:
    texto = texto.lower()
    texto = re.sub(r'[^\w\s]', '', texto)
    return re.sub(r'\s+', ' ', texto).strip()

def guardar_o_actualizar_productos(productos_extraidos, tienda, estado_orden, ip, ciudad, isp, tipo_conexion, sede, zip_code):
    conn = get_db_connection()
    cursor = conn.cursor()
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for prod_display in productos_extraidos:
        norm = normalizar_texto(prod_display)
        if not norm: continue
            
        cursor.execute("SELECT id, veces_exitoso, veces_rechazado FROM productos WHERE nombre_normalizado = %s", (norm,))
        row = cursor.fetchone()
        
        if row is None:
            exitoso = 1 if estado_orden == 'Exitosa' else 0
            rechazado = 1 if estado_orden != 'Exitosa' else 0
            cursor.execute("""
                INSERT INTO productos (nombre_display, nombre_normalizado, tienda, veces_exitoso, veces_rechazado, ultima_actualizacion)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """, (prod_display, norm, tienda, exitoso, rechazado, ahora))
            producto_id = cursor.fetchone()[0]
        else:
            producto_id = row[0]
            exitoso = row[1] + (1 if estado_orden == 'Exitosa' else 0)
            rechazado = row[2] + (1 if estado_orden != 'Exitosa' else 0)
            cursor.execute("""
                UPDATE productos 
                SET veces_exitoso = %s, veces_rechazado = %s, ultima_actualizacion = %s, nombre_display = %s
                WHERE id = %s
            """, (exitoso, rechazado, ahora, prod_display, producto_id))
            
        cursor.execute("""
            INSERT INTO historial_compras (producto_id, estado, ip, ciudad, isp, tipo_conexion, sede, zip_code, fecha)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (producto_id, estado_orden, ip, ciudad, isp, tipo_conexion, sede, zip_code, ahora))
            
    conn.commit()
    conn.close()

def extraer_datos_pdf(pdf_bytes, tienda, modelo_seleccionado):
    prompt = f"""
    Analiza este comprobante de orden digital de {tienda.capitalize()}. 
    1. Extrae únicamente la lista de nombres de los productos comprados (descarta precios, cantidades, totales, códigos de barras y subtotales). Si un texto junta dos productos, sepáralos.
    2. Busca y extrae el nombre de la sede o sucursal de la tienda (ej. "Midland Sam's Club") y el código postal (Zip Code). Si no aparecen, déjalos como null.
    
    Responde estrictamente en formato JSON válido con esta estructura:
    {{
      "tienda": "{tienda}",
      "sede": "nombre de la sucursal",
      "zip_code": "codigo postal",
      "productos": ["prod 1", "prod 2"]
    }}
    """
    try:
        if "gemini" in modelo_seleccionado:
            response = client.models.generate_content(
                model=modelo_seleccionado,
                contents=[types.Part.from_bytes(data=pdf_bytes, mime_type='application/pdf'), prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            texto_limpio = response.text.replace("```json", "").replace("```", "").strip()
            return json.loads(texto_limpio)
        else:
            lector_pdf = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            texto_pdf = "".join([pagina.extract_text() + "\n" for pagina in lector_pdf.pages])
            prompt_texto = f"{prompt}\n\nAquí tienes el texto extraído del comprobante:\n{texto_pdf}"
            response_alt = alt_client.chat.completions.create(
                model=modelo_seleccionado, 
                messages=[{"role": "user", "content": prompt_texto}],
                response_format={"type": "json_object"} 
            )
            texto_limpio = response_alt.choices[0].message.content.replace("```json", "").replace("```", "").strip()
            return json.loads(texto_limpio)
    except Exception:
        st.warning("⚠️ El modelo está saturado. Intenta con otro.")
        return {}

# ==========================================
# INTERFAZ Y LÓGICA DE STREAMLIT
# ==========================================
st.set_page_config(page_title="Validador de Compras", layout="wide")
init_db()

if "authenticated" not in st.session_state: st.session_state.authenticated = False

if not st.session_state.authenticated:
    col1, col2, col3 = st.columns([1, 1.2, 1])
    with col2:
        st.markdown("### 🔐 Acceso Restringido")
        with st.form("login_form"):
            username_input = st.text_input("Usuario")
            password_input = st.text_input("Contraseña", type="password")
            if st.form_submit_button("Ingresar", use_container_width=True):
                if username_input == ADMIN_USER and password_input == ADMIN_PASSWORD:
                    st.session_state.authenticated = True
                    st.rerun()
                else:
                    st.error("Credenciales incorrectas.")
    st.stop()

with st.sidebar:
    modelo_elegido = st.selectbox("Motor de IA:", ["gemini-3.5-flash-lite", "gemini-3.6-flash", "inclusionai/ling-3.0-flash-fin:free"])
    if st.button("Detener Consulta"): st.rerun()
    if st.button("Cerrar Sesión"): 
        st.session_state.authenticated = False
        st.rerun()

tab1, tab2, tab3 = st.tabs(["Carga de Archivos", "Asistente Predictivo", "Base de Datos"])

with tab1:
    c1, c2, c3 = st.columns(3)
    with c1:
        tienda_sel = st.selectbox("Tienda", ["walmart", "sams_club"])
        ip_compra = st.text_input("IP")
        sede_input = st.text_input("Sede")
    with c2:
        estado_orden = st.selectbox("Resultado", ["Exitosa", "Fallida/Con Rechazos"])
        ciudad_compra = st.text_input("Ciudad")
        zip_input = st.text_input("Zip Code")
    with c3:
        tipo_conexion = st.selectbox("Conexión", ["Residencial", "Datacenter/Cloud", "VPN", "Proxy"])
        isp_compra = st.text_input("ISP")
        
    uploaded_files = st.file_uploader("PDFs", type=["pdf"], accept_multiple_files=True)
    if uploaded_files and st.button("Procesar"):
        total_archivos = len(uploaded_files)
        progress_bar = st.progress(0)
        status_text = st.empty()
        productos_totales = 0
        
        for index, archivo in enumerate(uploaded_files):
            # Actualiza el texto para mostrar qué archivo se está procesando
            status_text.markdown(f"⏳ Procesando archivo {index + 1} de {total_archivos}: **{archivo.name}**...")
            
            with st.spinner(f"Extrayendo datos con {modelo_elegido}..."):
                datos = extraer_datos_pdf(archivo.read(), tienda_sel, modelo_elegido)
                
            productos = datos.get("productos")
            if productos:
                guardar_o_actualizar_productos(
                    productos, tienda_sel, estado_orden, ip_compra, ciudad_compra, 
                    isp_compra, tipo_conexion, datos.get("sede") or sede_input, datos.get("zip_code") or zip_input
                )
                productos_totales += len(productos)
                
            # Avanza la barra de progreso proporcionalmente
            progress_bar.progress((index + 1) / total_archivos)
            
        # Mensaje de éxito final con contadores
        status_text.success(f"✅ ¡Completado! {total_archivos} archivo(s) procesado(s) y {productos_totales} producto(s) registrado(s) en la base de datos.")
        time.sleep(3) # Opcional: Pausa breve antes de limpiar si lo deseas

with tab2:
    # Encabezado con botón para limpiar el chat
    col_title, col_btn = st.columns([4, 1])
    with col_title:
        st.markdown('<div class="section-title"><i class="fa-solid fa-robot" style="color:#3b82f6;"></i> Motor de Análisis de Riesgo</div>', unsafe_allow_html=True)
    with col_btn:
        st.write("") # Espaciador
        if st.button("🗑️ Borrar Chat", use_container_width=True):
            st.session_state.messages = []
            st.rerun()
            
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Mostrar historial de mensajes
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Input del usuario
    if user_prompt := st.chat_input("Ej: Jabón Zest. Tengo IP Residencial y enviaré a la sede de Midland (79706)."):
        st.session_state.messages.append({"role": "user", "content": user_prompt})
        with st.chat_message("user"):
            st.markdown(user_prompt)

        with st.chat_message("assistant"):
            # Obtener datos robustos de PostgreSQL
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.nombre_display, p.veces_exitoso, p.veces_rechazado, p.tienda,
                       (SELECT h.ip || ' (' || h.tipo_conexion || ') | Sede: ' || COALESCE(h.sede, 'N/A') || ' Zip: ' || COALESCE(h.zip_code, 'N/A') 
                        FROM historial_compras h WHERE h.producto_id = p.id ORDER BY h.fecha DESC LIMIT 1) as ultimos_datos
                FROM productos p
            """)
            registros = cursor.fetchall()
            conn.close()
            
            # Construir el resumen para el prompt
            inventario_resumen = ""
            for r in registros:
                total = r[1] + r[2]
                probabilidad = round((r[1] / total) * 100) if total > 0 else 0
                inventario_resumen += f"- {r[0]} | Éxitos: {r[1]} | Rechazos: {r[2]} | Probabilidad: {probabilidad}% | Última red/sede: {r[4]}\n"
            
            if not inventario_resumen:
                inventario_resumen = "Sin registros previos."

            # Prompt estructurado
            prompt_chat = f"""
            Eres un motor de análisis algorítmico de riesgo para compras online. Evalúa esta intención de compra del usuario: "{user_prompt}"

            Datos históricos de la Base de Datos:
            {inventario_resumen}

            Instrucciones estrictas:
            Sé ultraconciso. Cero rodeos, sin saludos ni introducciones amables. Ve directo al grano.
            Usa el siguiente formato exacto:

            **📊 RIESGO GLOBAL DE LA COMPRA:** [ALTO / MEDIO / BAJO]
            **⚡ ÍNDICE DE CONFIANZA DEL CARRITO (TCI):** [Calcula un % global de 0 a 100 basado en el cruce de los datos, red e historial]

            **🔴 PRODUCTOS DE ALTO RIESGO DE CANCELACIÓN:**
            (Listado de productos con historial negativo. Si no hay, escribe "Ninguno detectado".)
            - [Producto]: [X] negativos históricos | [Razón brevísima]

            **🟢 PRODUCTOS DE BAJO RIESGO / SEGUROS:**
            (Listado de productos con buen historial o sin alertas rojas.)
            - [Producto]: [X]% de probabilidad de éxito | [Motivo breve]
            """

            try:
                with st.spinner(f"Analizando riesgos con {modelo_elegido}..."):
                    if "gemini" in modelo_elegido:
                        response_stream = client.models.generate_content_stream(model=modelo_elegido, contents=prompt_chat)
                        def stream_gemini():
                            for chunk in response_stream:
                                yield chunk.text
                        respuesta_completa = st.write_stream(stream_gemini())
                    else:
                        response_alt = alt_client.chat.completions.create(
                            model=modelo_elegido,
                            messages=[{"role": "user", "content": prompt_chat}],
                            stream=True
                        )
                        def stream_openrouter():
                            for chunk in response_alt:
                                if chunk.choices and len(chunk.choices) > 0 and chunk.choices[0].delta.content is not None:
                                    yield chunk.choices[0].delta.content
                        respuesta_completa = st.write_stream(stream_openrouter())
                        
                    st.session_state.messages.append({"role": "assistant", "content": respuesta_completa})
                    
            except Exception as e:
                st.error("⚠️ El modelo está saturado en este momento. Utiliza un modelo distinto en el panel lateral o vuelve a intentar en unos segundos.")

with tab3:
    conn = get_db_connection()
    try:
        st.dataframe(pd.read_sql("SELECT * FROM productos", conn), use_container_width=True)
        st.dataframe(pd.read_sql("SELECT * FROM historial_compras", conn), use_container_width=True)
    except Exception:
        st.info("Sin datos.")
    finally:
        conn.close()
