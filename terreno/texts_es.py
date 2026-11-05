"""Textos fijos del informe (spec sec. 3 #13, sec. 5). Constantes: NO las edita el agente ni dependen del manifiesto.

El texto legal es BORRADOR: no esta validado por abogado (plan, restricciones globales; spec sec. 5).
"""
from __future__ import annotations

TITULO = "Informe de terreno preliminar"
SUBTITULO = "Relieve, orientación y confiabilidad del dato, a partir de Copernicus GLO-30"

# --- descargo (spec sec. 5) ---
DESCARGO = (
    "Evaluación preliminar basada en datos públicos. Los resultados deben validarse con levantamientos detallados y "
    "estudios de ingeniería antes de tomar decisiones."
)
FILTRO_DE_ESCRITORIO = "Este informe es un filtro de escritorio (desk screen), no un levantamiento."
BORRADOR_LEGAL = "Texto legal en borrador: pendiente de revisión por un abogado antes de mostrarlo a terceros."
NO_AVAL = "No se afirma aval oficial de Copernicus, ESA, JAXA ni de organismo chileno alguno."
AVISO_RESOLUCION = "Resolución real del dato: 30 m; un píxel (≈900 m²) es del orden de un lote urbano."

# --- "Qué este informe no cubre" (lista fija, spec sec. 3 #13 y sec. 5) ---
NO_CUBRE_TITULO = "Qué este informe no cubre"
NO_CUBRE_ITEMS = (
    "Plan regulador comunal (PRC), normativa OGUC, zonificación, usos de suelo y cabida.",
    "Deslindes y superficies legales; título de dominio, servidumbres, restricciones, derechos de agua y derechos mineros.",
    "Cotas de diseño, cortes y rellenos y volúmenes de movimiento de tierra.",
    "Geotecnia y suelos.",
    "Inundación, cotas inundables y drenaje dentro del predio.",
    "Remoción en masa como peligro: solo se señala de forma cualitativa dentro de la pendiente mayor a 30 %.",
    "Edificios vecinos, sombras de proyecto y vistas (viewshed).",
    "Aptitud solar energética (irradiancia).",
    "Curvas de nivel finas y archivos DXF/GeoJSON de curvas: las curvas del informe son solo indicativas.",
)

# --- atribuciones (spec sec. 5; textos literales de la licencia de GLO-30, version a confirmar) ---
ATRIBUCION_TITULO = "Atribución de los datos"
ATRIBUCION_GLO30 = (
    "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided "
    "under COPERNICUS by the European Union and ESA; all rights reserved"
)
NO_RESPONSABILIDAD_GLO30 = (
    "The organisations in charge of the Copernicus programme by law or by delegation do not incur any liability for any "
    "use of the Copernicus WorldDEM-30"
)
NOTA_GLO30_MODIFICADO = "Datos modificados: reproyectados a UTM, remuestreados a 30 m y procesados para este informe."
SENTINEL2_TEMPLATE = "Contains modified Copernicus Sentinel data {year}"  # solo si se usa una textura Sentinel-2
ATRIBUCION_WORLDCOVER = (
    "Cobertura de dosel y construido: © ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) "
    "processed by the ESA WorldCover consortium; licencia CC BY 4.0."
)
NOTA_AW3D30 = (
    "Comparación con ALOS World 3D 30 m (AW3D30), JAXA: solo se muestran estadísticas y un mapa de diferencia; el ráster "
    "de AW3D30 no se redistribuye. (El texto de atribución de JAXA está pendiente de revisión.)"
)

# --- reglas de honestidad que se repiten ---
CURVAS_INDICATIVAS = "Curvas de nivel indicativas, no para diseño."
CURVAS_SIN_DESCARGA = "Solo se muestran en este PDF: no se entregan como DXF ni GeoJSON."
NO_CONCLUYENTE = "no concluyente"
REVISION_HUMANA_C = (
    "Nivel C: revisión humana obligatoria antes de entregar este informe. Las cifras de pendiente puntuales se omiten "
    "(queda «no concluyente»)."
)
RUIDO_DEL_DATO = "El desnivel dentro del predio está dentro del ruido del dato."
TOLERANCIA_VERTICAL = "±4 m"
LE90_NOTA = "Error vertical absoluto de GLO-30: menor que 4 m (LE90); es el piso de tolerancia de toda cota mostrada."
PENDIENTE_REFERENCIA = (
    "Referencia de la fuente: error vertical relativo de GLO-30 menor que ~2 m hasta 20 % de pendiente y menor que ~4 m "
    "sobre 20 % (hipótesis de la spec, a calibrar contra ICESat-2/GEDI)."
)
ASOLEAMIENTO_ALCANCE = (
    "El DEM no incluye edificios vecinos: solo se modela el relieve (y las obstrucciones que el DSM capture) dentro de la "
    "ventana de trabajo alrededor del predio. No incluye cerros lejanos ni nubosidad. No es un estudio de sombras de proyecto."
)
HIDRICO_ALCANCE = (
    "Solo contexto de ladera: no se entrega drenaje dentro del predio. En cuencas endorreicas (altiplano, salares) este "
    "contexto no aplica y no se fuerza una dirección de escurrimiento; este POC no evalúa si el predio está en una."
)
PUNTAJE_ALCANCE = (
    "Orden relativo entre terrenos, no una medida física. La fórmula (pendiente media y rango de elevación) es una hipótesis."
)
SUPERFICIE_NOTA = "La superficie sale del polígono dibujado, no de un título de dominio."
SIN_SENTINEL2 = "Este POC no usa imagen satelital: el mapa muestra el sombreado del relieve (GLO-30)."
SELLO_NAMES = {"medibilidad_lote": "Medibilidad del lote", "confianza_dato": "Confianza del dato"}
SELLO_PREGUNTAS = {
    "medibilidad_lote": "¿se puede decir algo del lote con píxeles de 30 m?",
    "confianza_dato": "¿qué tan bueno es el dato satelital en este lugar?",
}
REGLA_CONFIANZA = (
    "El nivel final es el del peor factor (regla del eslabón más débil); un factor sin dato limita el nivel a B como máximo. "
    "La regla A/B/C es una hipótesis: sus cortes se ajustaron con mediciones en la Región Metropolitana y todavía no está "
    "validada."
)
