# Revisión de la rama del POC (959149c..HEAD = bf023d2)

Revisión de solo lectura hecha el 2026-10-30. Leí todo `terreno/`, `run_pilots.py`, `check_acceptance.py`, `run_agent_live.py`, el visor (`app.js`, `tour.js`), `LEDGER.md` y la spec. Hice además experimentos que solo leen: conversiones de geoide con pyproj, la caché real de huellas de `out/ñuñoa`, `agent_log.json` y el manifiesto de `out/agente_nunoa`, el diccionario de datos oficial de ATL08 v7 y el locale de Python. No modifiqué código ni las carpetas `out*`.

Escala de severidad, según el efecto sobre quien usa el informe:
- **Crítica:** cifras o niveles equivocados, incumplimiento de licencia o hueco de seguridad.
- **Importante:** error en un caso real plausible, o falla silenciosa.
- **Menor:** el resto.

## Hallazgos

| id | severidad | archivo:línea | hallazgo | escenario / evidencia | corrección sugerida |
|---|---|---|---|---|---|
| C1 | **Crítica** | `terreno/truth.py:184,193-194` (y `tests/test_truth.py:165,199`, LEDGER l.38) | **El filtro `subset_te_flag` de ATL08 está invertido.** El código exige que los cinco valores sean `== 0`. Según el diccionario oficial ATL08 v7 (pág. 1-26), `subset_te_flag` es `INTEGER_1(5,:)`, con `-1 = no_photon_data_within_geosegment`, `0 = no_terrain_photons_within_geosegment` y `1 = terrain_photons_present_within_geosegment`. Todo segmento con `h_te_best_fit` válido tiene fotones de terreno en al menos un subsegmento, así que **ninguna huella ATL08 puede pasar el filtro**. La validación de los criterios 4, 5 y 6 y el Tier 2 queda, sin decirlo, como "solo GEDI", aunque el informe y el manifiesto dicen "ICESat-2/GEDI". | Caché real `out/ñuñoa/cache/truth/*.json`: ATL08 tiene **0 de 295** huellas con quality 1, todas con `h_te_best_fit` válido (610-628 m elipsoidales) e incertidumbre de ~3,7 m (< 15 m). GEDI tiene 408 de 946 (43 %, cifra plausible). El fixture sintético codifica 0 = bueno, por eso los tests pasan. El "189 de 294 excluidos" se explica entero por este error más el filtro de GEDI. | Usar `ok &= _rows_all(sub == 1)` (o como mínimo ≥ 4 subsegmentos con 1 y ninguno en -1). Corregir el fixture a la semántica v7 y agregar un test con un valor real (`[1,1,1,1,1]` debe pasar). Corregir el LEDGER. Invalidar las cachés `truth/` (ver M1) y volver a correr los criterios 4-6. |
| I1 | Importante | `run_pilots.py:254,261,266` | **El arreglo nº 3 de e7527b3 (validar contra el ráster de contexto) quedó a medias.** `validate` del pipeline usa el contexto de ±1500 m. Pero `reference_slopes(res.raster, …)` (criterio 6), `evaluate_bias_correction(res.raster, …)` (criterio 5/Tier 2) y la validación de AW3D30 (`b_aligned_path`, que está en la malla de trabajo) siguen sobre el **ráster de trabajo** (bbox del predio ± 90 m). | En Ñuñoa casi ninguna huella cae en esa ventana: Tier 2 y la pendiente de referencia salen `validacion_insuficiente`/None, y el criterio 5 queda en FAIL o "insuficiente" por falta de código y no de datos. En los sitios de 300 m, GLO-30 se mide en ~3,3 km y AW3D30 en ~0,48 km. Son conjuntos de huellas distintos, así que comparar el RMSE de GLO-30 con el de AW3D30 (criterio 4) mezcla cosas distintas. | Pasar el ráster de contexto (exponerlo en `RunResult`) a ranking y Tier 2. Para AW3D30, alinearlo también sobre la ventana de contexto, o evaluar ambos DEM solo en las huellas comunes a las dos ventanas. |
| I2 | Importante | `terreno/agent_tools.py:375-379` (`_REQUIRES`) y `312-330` | **El agente puede bajar un C a B si omite `compare_sources`.** `score_confidence` solo exige `compute_terrain`. Si el agente salta `compare_sources`, o la llama *después* de `score_confidence` (está permitido: no está en `done`), el manifiesto queda con `comparison: no_disponible / "aw3d30 no solicitado"`. El factor de desacuerdo pasa a `sin_dato` y el nivel queda con tope B. | Un sitio con RMS GLO-30 vs AW3D30 > 6 m (C según la spec) sale B, y el PDF dice "No disponible: aw3d30 no solicitado". Que la etapa sea "opcional" debería depender de la disponibilidad de la fuente, no de lo que decida el modelo. La spec dice que el agente no elige recetas ni umbrales. | Hacer que `score_confidence` exija `compare_sources` en `done` (aceptando `no_disponible`), o ejecutar la comparación dentro de `score_confidence`. Rechazar `compare_sources` cuando el manifiesto ya existe. |
| I3 | Importante | `terreno/report.py:257,305,524` + `terreno/fetch.py:156`, `compare.py:141` | **El PDF rotula la fecha de *descarga* como "Fecha de adquisición del dato".** `acquired = date.today()` al bajar la ventana. El visor la rotula bien ("Descarga", `index.html:118`). | Un cliente lee "Fecha de adquisición del dato: 2026-10-30" en la hoja 1 y entiende que el relieve es de hoy. GLO-30 viene de TanDEM-X 2011-2015 (la licencia dice 2010-2014/2014-2018). La spec (sec. 3 #2) pide la fecha de adquisición real. | Rotularla "Fecha de descarga" y agregar una línea fija "Adquisición TanDEM-X 2011-2015 (según licencia/handbook)". Si se quiere más precisión, leer el `.xml` del tile. |
| I4 | Importante | `terreno/pipeline.py:247-251` | **Un fallo de la verdad de terreno (etapa solo de validación) rechaza el informe entero.** Cualquier `RejectRequest` de `fetch_footprints` termina en `_reject` y `rejection.json`: CMR caído, 401 de Earthdata, "granulo no disponible tras 3 intentos" o falta de `_netrc` a mitad de corrida. Si en cambio la excepción no es `RejectRequest` (p. ej. `OSError` de h5py), no se escribe `rejection.json` y `run_site` lo registra como "error". | En `run_pilots --live`, una caída de LP DAAC hace que un sitio válido salga "rechazado" y el criterio 1 (5×2 corridas válidas) falle por un servicio externo de validación. La spec dice que `sample_ground_truth` es "solo POC/validación". | Envolver `ground_truth` + `validate` como `compare`: `results.ground_truth_validation = {status: no_disponible, reason}` y seguir. Capturar también `OSError`. |
| I5 | Importante | `terreno/truth.py:331-334` | `_is_transient` solo reconoce un `exc.status >= 500` o los builtins `TimeoutError`/`ConnectionError`. Un 502 o un corte **durante la lectura h5py** sobre fsspec llega en la práctica como `OSError` de h5py (el callback de lectura pierde la excepción original), o como `aiohttp.ClientConnectionError`/`ServerDisconnectedError`, que no heredan de `ConnectionError`. Ninguno de los dos se reintenta. | El test del arreglo usa una excepción sintética con `.status`. El 502 real visto el 30-10 pudo ocurrir en `earthaccess.open` (sí cubierto), pero uno a mitad de la lectura de un gránulo de 168 MB no se reintenta y, por I4, tumba la corrida. | Recorrer `exc.__cause__`/`__context__`, tratar `OSError` de lectura remota y `aiohttp.ClientError` como transitorios, y acotar por URL. |
| I6 | Importante | `check_acceptance.py:347` + `terreno/ranking.py:44-49` | **El criterio 6 (ranking de pendiente) es casi tautológico.** Compara el plano ajustado al **DEM muestreado en las huellas** (`dem = truth + diffs`) con el plano de las huellas. Con residuos de ~2-5 m frente a diferencias de pendiente entre sitios de 3-25 %, el Spearman sale ~1 aunque el producto ordene mal. La pendiente que el informe entrega (`slope_mean_pct` del predio) nunca se ordena. | Daría PASS al criterio 6 aunque `compute_terrain` tuviera un error de escala de pendiente, porque esa pendiente no interviene. | Ordenar los sitios por `results.terrain.slope_mean_pct` (o por la pendiente de `compute_terrain` en la misma ventana) frente al plano de las huellas, y declarar la diferencia de escala. |
| I7 | Importante | `terreno/layers.py:100-111` + `confidence.py:47-53` | **Un fallo de red al leer EDM/FLM/WBM se traga y deja el factor 6 como `sin_dato`, que con la regla de tope B puede esconder un C.** No hay reintento propio: solo el `GDAL_HTTP_MAX_RETRY` de GDAL y un `except Exception` genérico. | Un predio en la orilla de un lago o en una zona rellenada con SRTM/ASTER (> 10 %, C), con un 503 transitorio del bucket, sale B con "parcial_o_sin_dato" en el manifiesto. El PDF muestra el factor como "sin dato (tope B)", sin aviso de que pudo ser C. | Reintentar y, si sigue fallando, rechazar o marcar C/revisión humana para los factores que sí pueden medirse (vacíos/agua). Reservar el tope B para lo que el POC no mide por diseño (dosel). |
| M1 | Menor | `terreno/truth.py:410-417` | La clave de caché de huellas es `sha1(url|bbox)`: no incluye la versión de las reglas de calidad. Se cachea `quality_flag` ya calculado y se escribe con `write_text`, que no es atómico. | Tras corregir C1, una caché persistente (`cache_dir` reutilizado) sigue sirviendo ATL08 con quality 0. Si se interrumpe a mitad de la escritura queda un JSON truncado y el siguiente `json.loads` revienta (no es `RejectRequest`). | Cachear las banderas crudas y calcular `quality_flag` al leer, o meter `QUALITY_RULES_VERSION` en la clave. Escribir en `.part` + `replace`. |
| M2 | Menor | `terreno/truth.py:189-190` | `msw_flag == 0` descarta también `127 = cannot_determine`, que según el diccionario aparece "only during daylight". Sumado a `layer_flag == 0`, elimina casi todo ATL08 diurno. | Aun corrigiendo C1, el rendimiento de ATL08 en sitios con pocas pasadas nocturnas puede quedar bajo 10 huellas. | Evaluar aceptar 127 (o usar solo `layer_flag`) e informar los conteos por bandera en `n_excluded_quality`. |
| M3 | Menor | `terreno/manifest.py:123-124` | La versión de AW3D30 está fija en "v3.1". OpenTopography sirve **v3.2 (enero de 2021**, DOI 10.5069/G94M92HB). | Trazabilidad del anexo (principio 2) con versión equivocada. | Poner "v3.2 (OpenTopography, DOI 10.5069/G94M92HB)", o leerla de la respuesta. |
| M4 | Menor | `terreno/bias_correct.py:117-126` | El split train/test agrupa por `(fuente, gránulo, haz)`. Los haces de un mismo gránulo ATL08 (par fuerte/débil a ~90 m, misma pasada y fecha) o los 8 haces GEDI de una órbita pueden quedar a ambos lados. | Errores correlacionados (misma fecha, misma vegetación, mismo sesgo de pasada) se filtran de train a test, y la ganancia del Tier 2 sale optimista. | Agrupar por `(fuente, gránulo)`. |
| M5 | Menor | `terreno/pipeline.py:230`, `layers.py:110`, `manifest.py:166-181` | Se cuelan en la parte hasheada textos variables: `comparison.reason` (puede traer la ruta absoluta del caché o el cuerpo HTTP de OpenTopography), `aux_layers.note` (mensaje de excepción). Las huellas no quedan registradas en `inputs` (lista de gránulos). | Dos corridas que fallan igual pero en carpetas distintas pueden dar hashes distintos. Con verdad activa, el hash cambia cuando la NASA publica gránulos nuevos aunque `inputs.hash` siga igual. | Normalizar los motivos (código + texto sin rutas). Agregar a `inputs` la lista de gránulos y su fecha de búsqueda. |
| M6 | Menor | `terreno/agent_tools.py:180`, `agent.py:181`, `pipeline.py:180` | Al empezar se borran manifest/rejection/flag/log, pero **no** `report.pdf`, `terreno.glb`, `layers/` ni `publicado/` de una corrida anterior. `run_agent_live.py` reutiliza `out/agente_<caso>`. | Corrida 2 en la misma carpeta con `render_pdf` → `status error`: `outcome = "informe"` apunta al PDF de la corrida 1. Una corrida rechazada deja el `report.pdf`/`publicado/` viejos junto a `rejection.json`. | Limpiar todos los entregables propios al iniciar la sesión y en `stop()`, o exigir una carpeta vacía. |
| M7 | Menor | `terreno/report_figs.py:391-393` | `fig_3d` imprime como etiquetas del eje z el mínimo y máximo de la *ventana* de trabajo, calculados al renderizar desde una ruta volátil, no del manifiesto. La vista 3D se muestra aunque la pendiente sea "no concluyente", mientras los perfiles se omiten por esa misma razón. | Números en el PDF que no están en el manifiesto (contra el principio de la Task 10). Inconsistente con la regla que el propio report.py se fija para los perfiles. | Quitar las etiquetas numéricas de z (o tomar elev_min/max del manifiesto) y aplicar la misma regla de omisión, o rotularla "vista cualitativa". |
| M8 | Menor | `terreno/config.py:41-44` | `subprocess.run(text=True)` decodifica con el locale (**cp1252** en esta máquina, `utf8_mode=0`, Python 3.14.4) en modo estricto. GDAL escribe en UTF-8. | Una ruta con "Í" (UTF-8 `C3 8D`) en un mensaje de GDAL da `UnicodeDecodeError` en vez del `RuntimeError` con stderr. Verificado: `'Í'.encode('utf-8').decode('cp1252')` → error. | `encoding="utf-8", errors="replace"`. |
| M9 | Menor | `terreno/video.py:58` | La validación de la URL es `startswith("http://127.0.0.1")`, que también acepta `http://127.0.0.1.evil.com/...` y `http://localhost@evil.com/`. | Solo es alcanzable desde el código del anfitrión (el agente no tiene tool de video). | Parsear con `urllib.parse` y comparar `hostname` en {127.0.0.1, localhost}. |
| M10 | Menor | `viewer/app.js:114`, `viewer/tour.js:21-24` | Sin `fecha` en la URL, el barrido de sombra del video usa la fecha del día del render. | El mismo sitio da videos distintos según el día. No afecta el hash, pero sí la "captura determinista". | Fijar la fecha (p. ej. solsticio o equinoccio, como en el PDF) en `viewer_site_url`. |
| M11 | Menor | `terreno/agent_tools.py:274-278` | `s.poly` se asigna antes de `fetch_dem`. Si el fetch falla con un error genérico (`status error`, reintentable), el agente puede reintentar `fetch_dem` con **otro** polígono: el "bloqueo del polígono" del LEDGER solo vale tras un éxito. | Bajo impacto: se usa el polígono del reintento, que queda registrado en el manifiesto. | Bloquear el polígono en el primer intento validado. |
| M12 | Menor | `terreno/agent_tools.py:375-379` | `render_pdf` puede ir antes de `build_glb`. El PDF rotula ×1,5 y luego `build_glb` usa la exageración que elija el agente (1-5). | El texto "el visor muestra el mismo rótulo" del PDF queda falso: los dos están rotulados, pero con valores distintos. | Exigir `build_glb` antes de `render_pdf` (el orden de la spec). |

**Totales:** 1 crítico, 7 importantes, 12 menores.

## Revisado y sin hallazgos (por área)

1. **Datums verticales.** Convención comprobada con pyproj y las mallas reales:
   - El desplazamiento EGM96→EGM2008 es H08 = H96 + N96 − N08. Ñuñoa: +0,120 m (N96 27,291, N08 27,171). Punto (0,0): **−0,064 m**. El LEDGER dice "+0,06" en (0,0): es un error de redacción; el código está bien.
   - El elipsoide se convierte con H = h − N (N08 en (0,0) = 17,225). Las aserciones de control son correctas.
   - La diferencia es b − a y el sesgo es DEM − verdad, coherentes en todo el código.
   - El manifiesto del agente en Ñuñoa da `datum_shift_mean_m 0,082`, plausible.
   - Según el diccionario v7, `h_te_best_fit` es relativo al elipsoide WGS84 (las demás alturas h_te_* dicen "above the WGS84 Ellipsoid"). GEDI `elev_lowestmode` también es elipsoidal. Que la red PROJ caiga a "ballpark" se detecta en los dos caminos.
2. **CRS y zona UTM.**
   - Regla −72° por centroide.
   - Medio píxel PixelIsPoint en `tile_extent`/`_px_window` y bordes entre tiles sin doble conteo.
   - Banda de 50° S.
   - Aserciones de CRS, unidades y píxel en cada etapa.
   - Rechazo del polígono en metros.
   - Malla alineada a múltiplos de 30 m.
3. **Filtros de huellas.** GEDI (`quality_flag==1`, `degrade_flag==0`, `elevation_bias_flag==0`) es razonable, con 43 % de aceptación real. Los flags 2-D se reducen bien con `_rows_all`. Las huellas usadas caen dentro de la ventana de contexto (buffer de 1 km < 1,5 km). El problema está en ATL08: ver C1 y M2.
4. **Determinismo y hash.**
   - `volatile` excluye fecha, rutas, tiempos y plataforma.
   - Las entradas se hashean por valores y georreferencia.
   - El JSON es canónico, con redondeo a 6 decimales estable al releer.
   - `_comparison_dict` quita las rutas.
   - La ventana de contexto no entra al hash.
   - Lo único que se cuela son los textos de error y la falta de gránulos en `inputs` (M5).
5. **Red y caché.**
   - Ventanas COG y AW3D30 escritas en `.part` + `replace`.
   - Clave de ventana exacta (tile + filas/cols).
   - La clave de API no queda en caché ni en el manifiesto, y se borra de los mensajes de error.
   - Reintentos de GDAL HTTP.
   - Los problemas encontrados son I4, I5, I7 y M1.
6. **Cifras desde el manifiesto y gating C/no concluyente.**
   - `report_fields` es la única fuente.
   - El hash se verifica antes de renderizar.
   - El nivel C oculta pendiente, orientación, puntaje, perfiles y diferencia de pendiente en el PDF; en el visor (`noSlope`) y en las capas PNG transparentes también.
   - La tarjeta del video toma el nivel y los píxeles del manifiesto.
   - Las excepciones son M7 (eje z) y I3 (rótulo de fecha).
7. **Seguridad del agente.**
   - `can_use_tool` deniega por defecto y en la corrida real se invoca para las 9 llamadas (verificado en `out/agente_nunoa/agent_log.json`).
   - `tools=[]`, `setting_sources=[]`, `strict_mcp_config`, `disallowed_tools`.
   - Esquemas con `additionalProperties:false`, revalidados dentro de `_dispatch_inner` (defensa en profundidad).
   - Claves de ruta, URL o CRS prohibidas a cualquier profundidad.
   - `site` del visor sin esquema, `..` ni `//`.
   - `publish_viewer` copia por lista blanca y sanea el manifiesto.
   - Los únicos huecos son de lógica de orden (I2, M11, M12), no de acceso.
8. **Licencias y atribución.**
   - No hay FABDEM, FathomDEM ni GEDTM30 en el código.
   - El ráster AW3D30 queda solo en la caché local y no se publica: `publicado/` lleva solo GLB, capas y manifiesto de GLO-30. `extra.b_aligned_path` y `diff_map` se eliminan.
   - La atribución y la frase de no responsabilidad de GLO-30 aparecen en el PDF, el visor y el panel del video.
   - La atribución de Sentinel-2 solo sale si se usa textura.
   - Lo pendiente es solo lo ya declarado (texto de JAXA, versión de la licencia de GLO-30) más M3.
9. **Estadística.**
   - Bordes de la regla A/B/C idénticos a la tabla de la spec (≥25/≥9 px; >16/≥8 m; <20/≤30 %; <3/≤6 m; <10/≤50 %; <2/≤10 %; <3/≤6 m).
   - Peor factor con tope B por `sin_dato`.
   - Fórmulas de RMSE, MAE y sesgo correctas.
   - Muestreo bilineal en centros de celda correcto.
   - Rangos promedio en empates en Spearman.
   - Tier 2 con mediana en train y RMSE solo en test.
   - Veredicto `tier2_verdict` coherente.
   - Lo que falla son I1, I6 y M4.
10. **Windows.**
    - Rutas con ñ: el CLI de GDAL funciona (corridas reales).
    - URL del video percent-encoded.
    - `PYTHONIOENCODING=utf-8` en los workers.
    - `_netrc` de earthaccess.
    - JSON y textos con `encoding="utf-8"`.
    - `GDAL_DATA`/`PROJ_DATA` de QGIS solo en el subproceso.
    - Queda M8.

## No pude verificar

- Si el 502 real de LP DAAC llega por h5py como `OSError` sin causa (base de I5). No lo reproduje sin descargar gránulos.
- Si la AW3D30 que entrega OpenTopography declara correctamente su registro (Area vs Point) frente a GLO-30 (Point). Un error de medio píxel (15 m) inflaría el RMS en ladera (Viña: ~25 % → ~3,7 m). Sugiero medir el desplazamiento por correlación en Viña.
- Las tablas de códigos EDM y FLM contra el Copernicus DEM Product Handbook v5.0: no leí el handbook. Sí confirmé que el 0 % de vacíos de los pilotos es consistente con EDM 1 = sin editar.
- La semántica exacta de `elevation_bias_flag` y `degrade_flag` de GEDI L2A v2: la tomé del LEDGER, no la revisé en el diccionario.
- El resto de los flags de ATL08 en datos reales (`layer_flag`, `msw_flag`), porque la caché solo guarda el `quality_flag` final.
- No ejecuté pytest. El contenido de `out/` es de corridas en curso o anteriores y no se usó para afirmar nada salvo los conteos de C1 y el log del agente.

Fuentes externas: diccionario de datos ATL08 v7 (https://nsidc.org/sites/default/files/documents/technical-reference/icesat2_atl08_data_dict_v007.pdf, págs. 1-18, 1-23 y 1-26); anuncio de OpenTopography sobre AW3D30 v3.2 (https://opentopography.org/news/updated-alos-world-3d-dem-now-available-through-opentopography).
