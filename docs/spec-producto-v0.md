# SPEC-PRODUCTO-v0 — Informe de Terreno Preliminar

Estado: **borrador para revisión del fundador. No se construye nada hasta aprobarlo.**
Fecha: 2026-10-23. Fuentes: `INFORME-RESEARCH.md`, `VERIFICACION-CIFRAS.md`, `datos_publicos/FUENTES.md`, `PLAN-FASE2.md`, `reports/Opciones técnicas bajo 30 m.md` y las notas A1-A4.
Convención: **[hipótesis]** = propuesta mía a calibrar o validar; sin marca = viene de las notas. Precios y plan comercial quedan fuera de este documento.

---

## 1. Propósito y cliente

**Qué es.** Un informe automático, en español, sobre un terreno de Chile dibujado como polígono: cómo es el relieve, hacia dónde mira, cuánto sol y sombra recibe por relieve, y **qué tan confiable es cada cifra**. Va acompañado de una vitrina: visor web 3D interactivo, video corto de 30-60 s y enlace compartible.

**Qué no es.** No es un plano topográfico, ni una cabida, ni un estudio de riesgo. Es una evaluación preliminar basada en datos públicos que ayuda a decidir si vale la pena pagar un levantamiento.

**Cliente de v0 (en este orden):** desarrolladores inmobiliarios pequeños y medianos, corredores de terrenos y estudios de arquitectura. Otros segmentos (energía, consultoras ambientales, municipios, agro, educación) se evalúan después del POC; la spec no los bloquea, pero tampoco se diseña para ellos.

**Pregunta que responde el informe:** "¿Este terreno es plano, suave o empinado; mira al sol; y qué tan seguro estoy de eso?" — no "¿cuántos m³ muevo?".

**Principios de diseño**
1. Mejor decir "no concluyente" que dar un número falso.
2. Todo valor se rastrea hasta un dataset, versión y fecha.
3. El dato manda el nivel: la resolución real (30 m) se declara en cada página.
4. El 3D es la vitrina; la tabla y el nivel de confianza son el producto.

---

## 2. Niveles de calidad de datos

| Tier | Insumo | Qué habilita | Condición para activarlo |
|---|---|---|---|
| **1 — Gratis global** | GLO-30 (base) + AW3D30 (comparación) + capas auxiliares de GLO-30 (HEM/EDM/FLM/WBM si están disponibles) + Sentinel-2 como textura | Todo el informe v0 con tolerancia declarada (±4 m LE90 como piso) | Es el nivel por defecto |
| **2 — DTM mejorado** | Tier 1 más corrección de sesgo contra ICESat-2/GEDI y máscaras de bosque/construido (y, solo si la licencia se aclara, GEDTM30 como capa de incertidumbre) | Misma salida con menor sesgo declarado | Solo si el POC demuestra **ganancia medible** (criterio en sección 8). Si no, se queda Tier 1 y se documentan los límites |
| **3 — Dato fino cotizado por proyecto** | WorldDEM Neo (5 m, 1,4 m LE90 absoluto), AW3D Enhanced, dron/LiDAR local, o levantamiento del mandante | Pendiente y perfiles utilizables; recién ahí tiene sentido hablar de curvas finas | Cotización caso a caso; v0 no lo ejecuta, solo lo ofrece como "siguiente paso" en el informe |

No entran a ningún tier: FABDEM/FathomDEM (no comercial), super-resolución con IA, estéreo gratis ASTER/SPOT, ALOS RTC 12,5 m.

---

## 3. El Informe v0, sección por sección

Qué significa "tier mínimo": el nivel de datos sin el cual la sección no se genera. "Honestidad" = regla que el informe debe cumplir al mostrarla.

| # | Sección | Qué muestra | Tier mín. | Regla de honestidad |
|---|---|---|---|---|
| 1 | **Ficha del terreno** | Polígono, superficie, coordenadas, CRS, mapa satelital (Sentinel-2) con el polígono, rol/dirección si el cliente los aporta | 1 | La superficie sale del polígono dibujado, no de un título; decirlo |
| 2 | **Calidad del dato y nivel de confianza (A/B/C)** | Fuente (GLO-30), resolución real (30 m), fecha de adquisición, factores que determinaron el nivel, número de píxeles dentro del polígono | 1 | Va en la página 1, antes de los resultados. Nunca se omite |
| 3 | **Elevación** | Mínima, máxima, media y rango dentro del predio, en m s. n. m. (EGM2008) | 1 | Se muestra con tolerancia (±4 m como piso). Si el rango es menor a ~2×LE90 (≈8 m) [hipótesis] se dice que el desnivel está dentro del ruido del dato |
| 4 | **Pendiente por clases gruesas** | % del predio en clases <5 %, 5-15 %, 15-30 %, >30 % [hipótesis de cortes, tomados del ejemplo de A3] con mapa | 1 | Rangos, no cifras puntuales. "No concluyente" si el predio tiene menos de ~3×3 píxeles (≈90×90 m) o si la regla de confianza lo pide |
| 5 | **Orientación (aspect)** | Orientación predominante y rosa de orientaciones | 1 | En terreno casi plano es ruido: decir "sin orientación predominante" bajo un umbral de pendiente [hipótesis: <3 %] |
| 6 | **Puntaje de complejidad del relieve** | Escala de 1 a 7 (idea de Regrid) combinando pendiente y rango [hipótesis de fórmula] | 1 | Se presenta como orden relativo entre terrenos, no como medida física |
| 7 | **Asoleamiento y sombra por relieve** | Trayectoria solar por estación, perfil de horizonte, horas de sol directo por relieve lejano | 1 | El DEM no incluye edificios vecinos: decir que solo se modela el relieve y las obstrucciones que el DSM capture. No es un estudio de sombras de proyecto |
| 8 | **Perfiles de elevación** | 2-3 cortes (norte-sur, este-oeste, a elección) | 1 | Eje vertical con la banda de incertidumbre dibujada |
| 9 | **Curvas de nivel indicativas** | Solo en el PDF, equidistancia mínima 5-10 m [hipótesis, siguiendo INFORME] | 1 | Rotuladas "indicativas, no para diseño". Sin DXF/GeoJSON descargable |
| 10 | **Contexto hídrico de ladera** | Dirección general de escurrimiento a escala de ladera/cuenca, no dentro del predio | 1 | Solo contexto. No se entrega drenaje intra-predio. Cuencas endorreicas (altiplano, salares): se declara "no aplicable" en vez de forzar un drenaje |
| 11 | **Comparación de fuentes** | Diferencia GLO-30 vs AW3D30 (promedio y pendiente), mapa de desacuerdo | 1 | Si la diferencia es alta, el informe lo dice con palabras y baja el nivel |
| 12 | **Visor 3D y video** | Enlace al visor interactivo y al video (sección 6) | 1 | Exageración vertical siempre rotulada en pantalla y en el PDF |
| 13 | **Qué este informe no cubre** | Lista fija de límites (sección 5) | 1 | Obligatoria y no editable por el agente |
| 14 | **Anexo de trazabilidad** | Dataset, versión, fecha, enlace, CRS, tamaño de píxel, hash de la receta, versión de software | 1 | Sale del manifiesto generado por código, no del texto del agente |

**No incluido en v0 y por qué**

| Excluido | Motivo |
|---|---|
| Cut & fill y volúmenes de movimiento de tierra | Error crece con resolución y pendiente; con 30 m es falsa precisión (A3, INFORME) |
| Curvas finas, DXF/GeoJSON de curvas | Suavidad aparente que el dato no tiene |
| Cabida / zonificación PRC / OGUC | Depende de interpretar normativa comuna por comuna, no del DEM; fase posterior |
| Inundación, cotas inundables, HAND | No reemplaza un modelo hidráulico; riesgo alto |
| Remoción en masa como peligro | Solo cabe como alerta cualitativa dentro de pendiente (>30 %); no se presenta susceptibilidad |
| Aptitud solar energética (irradiancia) | `r.sun` asume cielo despejado salvo que se aporten coeficientes; sobrestima en el sur [INFORME] |
| Vistas (viewshed) | A3 lo ubica en fase 2 (exige modelar edificios); INFORME lo había puesto como MVP. Quedó fuera de v0 por riesgo en Windows y por falta de edificios: ver decisión 7 |
| Super-resolución con IA, STL/3MF para impresión | Alucinación de relieve; maqueta física es otra línea de producto |
| Suelos, geotecnia, deslindes, cotas de diseño | Fuera del alcance de un DEM |

---

## 4. Regla de confianza A/B/C

**Todo lo de esta sección es una hipótesis a calibrar en la Fase E** contra ICESat-2/GEDI y los levantamientos que se logren. Los umbrales no están medidos para Chile; cada uno indica de qué nota sale.

**Idea:** el nivel final es el **peor** de los factores (regla del eslabón más débil) [hipótesis]. Además se calcula un nivel por sección (ej. la pendiente puede ser C mientras la elevación es B).

| Factor | A | B | C | Origen del umbral |
|---|---|---|---|---|
| Píxeles de 30 m dentro del predio (n = superficie / 900 m²) | n ≥ 25 | 9 ≤ n < 25 | n < 9 ("no concluyente" en pendiente/aspecto) | 3×3 píxeles, A3; 1 ha ≈ 11 píxeles, A2 |
| Rango de elevación del predio frente al error relativo medido (≈2 m) | > 8 m | 4-8 m | < 4 m (desnivel dentro del ruido) | Calibrado RM 2026-10-31 (antes 16/8 m por 2×LE90): error relativo de GLO-30 en el lote vs Pléiades/SPOT 0,7-2,6 m en 5 lotes |
| Pendiente media en el predio y su entorno inmediato | < 20 % | 20-30 % | > 30 % | GLO-30 relativo <2 m hasta 20 % y <4 m sobre 20 %; INFORME: error se amplifica sobre ~12-15° |
| Desacuerdo GLO-30 vs AW3D30 (RMS sin el desfase medio, tras alinear datums) | < 3 m | 3-6 m | > 6 m | Orden del RMSE 3,5-4 m de ambos DEM vs ICESat-2 (Lund). Calibrado RM 2026-10-31: el desfase medio es sesgo propio de AW3D30 frente a ICESat-2/GEDI y se quita |
| Cobertura de dosel/construido (altura de dosel Meta/WRI o ETH; huellas de edificios) | < 10 % del predio | 10-50 % | > 50 % | FABDEM/GEDTM30 cambian de régimen en 10 % y 50 % de cobertura arbórea |
| Vacíos, rellenos y agua (EDM/FLM/WBM): % de píxeles rellenados con ASTER/SRTM/GMTED o marcados vacío/aplanado | < 2 % | 2-10 % | > 10 % | A2 sec. 4 (parte de GLO-30 no proviene de TanDEM-X) |
| Incertidumbre propia (HEM, percentil 90 en el predio) | < 3 m | 3-6 m | > 6 m | HEM = desviación estándar por píxel [hipótesis de corte] |

**Reglas de uso**
- Nivel C ⇒ revisión humana obligatoria antes de entregar, y el informe omite las cifras de pendiente puntuales (queda "no concluyente"). También ante cualquier cifra absoluta que el cliente quiera usar.
- Nivel A no significa "exacto": significa "el dato es apto para screening con la tolerancia declarada".
- La calibración se declara cumplida si los predios C muestran errores contra ICESat-2/GEDI sistemáticamente mayores que los A y el ranking de pendiente entre sitios coincide con el de la referencia (criterio en sección 8). Si no discrimina, se cambian los factores, no el criterio.

---

## 5. Descargos y trazabilidad (copiados en espíritu de Equator y LandBenchmark)

**Texto base (redacción final a cargo de abogado; no está validada legalmente):**
- "Evaluación preliminar basada en datos públicos. Los resultados deben validarse con levantamientos detallados y estudios de ingeniería antes de tomar decisiones" (Equator).
- "Este informe es un filtro de escritorio (*desk screen*), no un levantamiento" (LandBenchmark).
- Cada valor nombra dataset, versión, enlace y fecha; lo que no se puede medir se marca "no medible" con el motivo, en vez de estimarse (LandBenchmark).
- Lista fija **"Qué este informe no cubre"** (idea del checklist humano de LandBenchmark: título, servidumbres, zonificación, restricciones, derechos de agua/minerales). Adaptación a Chile [hipótesis]: PRC y normativa OGUC, deslindes y superficies legales, cotas de diseño y volúmenes, geotecnia, inundación, remoción en masa, edificios vecinos y sombras de proyecto.
- Aviso de resolución: "Resolución real del dato: 30 m; un píxel (≈900 m²) es del orden de un lote urbano".
- No se afirma aval oficial de Copernicus, ESA, JAXA ni de organismo chileno alguno.

**Atribuciones obligatorias (texto de las licencias leídas en las notas):**
- Copernicus GLO-30 (datos modificados): "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved", más la frase de no responsabilidad al redistribuir. Confirmar la versión vigente.
- Sentinel-2: "Contains modified Copernicus Sentinel data [año]".
- OSM u otros ODbL: atribución, y revisar share-alike antes de usarlos (pendiente de verificación).

---

## 6. La vitrina

**Visor web 3D** (stack elegido: three.js + GLB propio generado por el pipeline; overlays en GeoJSON proyectado).
- Terreno 3D con exageración vertical ajustable (rótulo siempre visible: "Exageración ×N").
- Polígono del terreno resaltado; textura Sentinel-2 10 m (sin teselas de Google/Bing/Esri).
- Interruptor de capas: pendiente por clases, orientación, curvas indicativas, sombreado.
- Deslizador de fecha y hora con sombra y trayectoria solar (SunCalc).
- Panel fijo "Fuente y límites": GLO-30, 30 m, nivel A/B/C, descargos.
- Enlace de solo lectura, sin datos personales del cliente en la URL.
- Publicación estática en Cloudflare Pages (límite de 25 MiB por archivo ⇒ GLB con compresión Draco/meshopt; tamaño real sin medir). GitHub Pages solo para demos.

**Video de 30-60 s.** Recorrido: panorama del sector → acercamiento al polígono → pasada con exageración vertical → barrido de sombra del día → tarjeta final con nivel de confianza y fuente. Debe mostrar en pantalla la exageración y el nivel de confianza. Generación: captura del visor con Playwright + ffmpeg (misma escena) o script `bpy` en Blender; elegir en el POC (decisión 3). Google Earth Studio queda descartado (no licencia uso comercial).

**Lo que la vitrina no hace:** mezclar fotografía 3D de alta resolución con el DEM de 30 m, ni ofrecer STL/DXF en v0.

---

## 7. Arquitectura de automatización

**Principio:** el agente (Claude) no calcula ni elige el CRS; **selecciona y parametriza recetas versionadas** de una lista cerrada, y redacta texto únicamente desde campos ya validados. (Diseño propuesto en A4/INFORME; no probado en producción.)

**Flujo:** polígono → validación (dentro de Chile, tamaño razonable) → `fetch_dem` (tiles por ventana COG, verificados contra `tileList.txt`; mosaico si cruza tiles) → `reproject` (UTM 18S/19S derivado por código del centroide) → `compute_terrain` (pendiente, aspect, hillshade, curvas) → `compare_sources` (AW3D30) → `sample_ground_truth` (ICESat-2/GEDI, solo en POC/validación) → `score_confidence` → `build_glb` → `render_pdf` → `publish_viewer` → manifiesto.

**Lista cerrada de tools** (nombres tentativos): `fetch_dem`, `reproject`, `compute_terrain`, `compare_sources`, `score_confidence`, `build_glb`, `render_pdf`, `publish_viewer`, `reject_request`, `request_human_review`. Sin Bash, sin edición de archivos ni acceso web libre para el agente: `disallowed_tools` explícito más `can_use_tool` que rechaza por defecto (`allowed_tools` solo no restringe). Se descarta `execute_code` de los MCP de QGIS existentes.

**Aserciones por paso (fallan duro, no silenciosas):**
- CRS del ráster == esperado; unidades en metros; tamaño de píxel dentro de tolerancia.
- Porcentaje de nodata bajo umbral; rango de elevación físicamente plausible.
- El polígono cae dentro del bbox descargado; zona UTM coherente con el centroide (casos de borde en 72° O se resuelven por regla explícita, no por el agente).
- Datum vertical declarado (EGM2008) y, si se compara con ICESat-2, conversión elipsoide ↔ geoide asertada.
- Toda salida lleva manifiesto JSON (CRS, resolución, hash, versiones); el informe lee solo del manifiesto.
- Dos corridas con la misma entrada producen el mismo hash de resultados.

**El rechazo es una acción de primera clase** (`reject_request(reason)`, registrada): tile no publicado o fuera de cobertura, polígono fuera de Chile o demasiado grande, nodata excesivo, aserción fallida, o relieve dentro del ruido del dato. No existe camino que "adivine" para completar.

**Revisión humana:** obligatoria en nivel C o ante cualquier cifra absoluta; en el POC, el fundador revisa los ocho informes piloto. Tiempo de revisión por informe se mide (insumo para la fase comercial).

**Versiones fijas:** QGIS 3.44 LTR (la máquina tiene 3.36.1) y GDAL/WhiteboxTools con versión anotada en cada manifiesto. GRASS (`r.sun`, `r.viewshed`) solo detrás de un test canario.

---

## 8. Criterios de aceptación del POC

El POC pasa si se cumplen **todos**:

1. **Reproducible de punta a punta:** desde un polígono hasta PDF + GLB + visor + video, en los ocho sitios piloto RM, con el mismo hash de resultados en dos corridas.
2. **Cero errores silenciosos de CRS:** una batería de pruebas con entradas deliberadamente malas (CRS equivocado, bbox invertido, polígono en el límite de zona UTM) debe fallar o rechazar, nunca producir un informe.
3. **Desacuerdo GLO-30 vs AW3D30 reportado en cada sitio**, con cifra y mapa, y consistente con el orden de magnitud de las especificaciones.
4. **Comparación contra ICESat-2/GEDI en cada sitio piloto:** nº de huellas, sesgo, MAE y RMSE de GLO-30 (y de AW3D30). Si hay menos de ~10 huellas en un sitio [hipótesis], se declara "validación insuficiente" en lugar de calcular. Además, comparación contra las nubes de referencia SPOT 2014 y Pléiades 2016 (OpenTopography, solo validación interna) en los pilotos que cubren, y contra cualquier levantamiento público que se obtenga.
5. **Veredicto sobre el Tier 2:** se promueve solo si la corrección reduce el RMSE frente a GLO-30 crudo en al menos 10 % en al menos 3 de 8 sitios [umbral heredado de 3 de 5; recalibrar con los 8 pilotos RM], medido contra referencia independiente [hipótesis de umbral]; si no, queda documentado como límite.
6. **Regla A/B/C evaluada:** los niveles asignados discriminan (error mayor en C que en A) y el ranking de sitios por pendiente coincide con el de la referencia, o se documenta que no discrimina.
7. **Informe legible por un no experto:** 3-5 personas ajenas al proyecto leen un informe y contestan correctamente preguntas simples (¿es plano o empinado?, ¿qué tan seguro es?, ¿qué no cubre el informe?) [hipótesis de protocolo].
8. **Licencias:** textos de GLO-30 vigente, AW3D30 y GEDTM30 leídos y archivados; atribuciones correctas en el informe.
9. **Medición de tiempo y cómputo reales** por informe (hoy son supuestos), incluido el render del video.

---

## 9. Sitios piloto (Región Metropolitana, solo datos públicos)

[Reemplazado 2026-10-30. Decisión del fundador: foco en la Región Metropolitana y uso exclusivo de información pública; los cinco pilotos originales se retiraron. Los pilotos de regiones (Viña/Reñaca, Puerto Varas, Coquimbo) quedan pausados en `poc/data/sites_regiones_pausados.json`.]

Son 8 **lotes de prueba estándar** (cuadrados abstractos, no predios reales) elegidos con una regla pública y reproducible (`poc/tools/elegir_pilotos_rm.py`): comunas DPA 2023, pendiente de GLO-30 y huellas de las nubes de referencia de OpenTopography (SPOT 2014, Pléiades 2016). Ningún sitio tiene elevación medida propia: la validación depende de ICESat-2/GEDI y de las nubes SPOT/Pléiades (solo uso interno).

| # | Clave | Comuna referencial (DPA) | Tipo de terreno | Lote | Pendiente media GLO-30 (ventana) | Referencia fina | Qué prueba |
|---|---|---|---|---|---|---|---|
| 1 | `rm01_plano_urbano` | Ñuñoa | Plano urbano consolidado | 800 m² | 2,7 % | SPOT | Edificios en DSM; lote chico (~1 píxel, "no concluyente") |
| 2 | `rm02_plano_periurbano` | Maipú | Plano periurbano poniente | 1,5 ha | 2,6 % | SPOT | Plano abierto; control |
| 3 | `rm03_piedemonte_oriente` | Peñalolén | Piedemonte oriente | 1,0 ha | 14,9 % | Pléiades + SPOT | Pendiente media; lote mediano |
| 4 | `rm04_piedemonte_alto` | Lo Barnechea | Piedemonte alto | 6,0 ha | 20,9 % | Pléiades + SPOT | Pendiente, drenaje, vacíos |
| 5 | `rm05_piedemonte_sur` | Puente Alto | Piedemonte sur | 1,8 ha | 18,3 % | Pléiades + SPOT | Pendiente media al sur |
| 6 | `rm06_cerro_isla_urbano` | Huechuraba | Cerro isla urbano | 1.000 m² | 48,4 % | SPOT | Pendiente fuerte con lote chico |
| 7 | `rm07_lomaje_norte` | Colina (Chicureo) | Lomaje norte | 8,0 ha | 8,5 % | ICESat-2/GEDI | Lomaje suave sin nube fina |
| 8 | `rm08_precordillera` | Pirque | Precordillera con vegetación | 7,0 ha | 52,1 % | ICESat-2/GEDI (y SPOT: la caja entra en su huella) | Sesgo de vegetación del DSM |

Tamaños mezclados a propósito: 2 lotes urbanos chicos (~800-1.000 m²), 3 medianos (0,5-2 ha) y 3 grandes (5-9 ha). La vegetación del piloto 8 no se verifica con capas públicas de cobertura (GLO-30 es DSM): se confirma al comparar con ICESat-2/GEDI. `sites.json` guarda para cada lote el polígono (EPSG:4326), el área, las banderas de cobertura de referencia y las cifras de la selección.

---

## 10. Decisiones abiertas para el fundador

Cada una con mi recomendación por defecto.

1. **Versión de QGIS/herramientas.** ¿Fijar 3.44 LTR o saltar a 4.x? *Recomendado: 3.44 LTR fijado (instalar sobre la 3.36.1 actual); evaluar la LTR 4.x cuando esté confirmada.*
2. **Visor web.** ¿three.js con GLB propio, MapLibre o Cesium? *Recomendado: three.js (menos piezas, sin dependencia de teselas de terceros).*
3. **Video.** ¿Captura del visor (Playwright + ffmpeg) o Blender? *Recomendado: captura del visor por consistencia; probar WebGL headless en Windows en la primera semana y pasar a Blender si falla.*
4. **Pilotos.** [Resuelto 2026-10-30] El fundador fijó 8 pilotos en la Región Metropolitana con solo datos públicos (sección 9); los de regiones quedan pausados.
5. **Pedir datos finos en paralelo.** ¿Lanzar solicitudes de levantamientos (DOM/mandantes) y de LiDAR regional (Ley de Transparencia) ahora? *Recomendado: sí, 3-5 solicitudes desde ya, sin bloquear el POC.*
6. **Verificación de licencias y revisión legal.** ¿Cuándo? *Recomendado: yo leo y archivo los textos de licencia durante la semana 1 y escribo a Copernicus/JAXA; el abogado entra antes de mostrar el informe a terceros, no antes del POC.*
7. **Qué módulos arriesgados entran.** ¿Viewshed y contexto hídrico en v0? *Recomendado: contexto hídrico sí (solo a escala de ladera, con "no aplicable" en cuencas endorreicas); viewshed no hasta fase 2.*
8. **Convenciones del informe.** ¿Cotas en m s. n. m. (EGM2008) y pendiente en % con grados entre paréntesis? ¿CRS UTM WGS 84 (32718/32719) declarado, sin SIRGAS-Chile? *Recomendado: sí a ambas; usar SIRGAS-Chile 2010 (8950/8951) solo si se cruza con cartografía oficial.*
