Fecha de la corrida: 2026-10-31; validación de la regla con 24 sitios: 2026-11-04. Solo datos públicos; los 8 lotes son lotes de prueba estándar elegidos con `poc/tools/elegir_pilotos_rm.py` (DPA 2023, GLO-30 y huellas públicas), no predios reales. Reemplaza la versión del 2026-10-23 (5 pilotos retirados).

**Puerta de salida a lo comercial: NO cumplida.** Se cumplen 2 de 4 ítems (sección 6). Lo que falta: la lectura legal, la prueba con lectores y decidir qué hacer con el nivel final (la regla del sello del dato quedó validada con 24 sitios, sección 4; el nivel final mezclado no).

## 1. Resumen

1. **Funciona y es reproducible.** Los 8 pilotos corren de punta a punta (polígono → PDF + GLB + visor + video de 45 s) y las dos corridas de cada sitio dan el mismo hash. Ahora incluye AW3D30 e ICESat-2/GEDI reales.
2. **Ya sabemos cuánto se equivoca GLO-30 en Santiago.**
   - Contra Pléiades 5 m (referencia interna, 3 pilotos del piedemonte): RMSE de **1,3 a 1,5 m dentro del lote** y de 1,7 a 3,5 m en el entorno; sesgo bajo 1 m; error de pendiente menor a 2°.
   - Contra ICESat-2/GEDI (8 pilotos): RMSE de 2,5 a 3,2 m en terreno plano y de 5 a 10,6 m en ladera y precordillera.
   - Es mejor que lo que suponía la spec (3,5-4 m) en terreno plano y en el piedemonte medido con Pléiades.
3. **La corrección de sesgo (Tier 2) no se promueve:** baja el error ≥ 10 % solo en 2 de 8 sitios. El producto queda en GLO-30 crudo con tolerancia declarada.
4. **La regla A/B/C se validó con 24 sitios (8 pilotos + 16 de validación), con una regla escrita antes de correr.** El sello de **confianza del dato** sí ordena el error: A 1,98 m (8 sitios), B 5,40 m (9), C 7,58 m (7); correlación de rangos 0,73 (mínimo exigido 0,5). El **nivel final** (el peor de los dos sellos) **no** discrimina: A 1,04 m (2 sitios), B 5,72 m (8), C 4,98 m (14), porque el sello de medibilidad del lote marca C a lotes llanos y chicos que tienen poco error (sección 4).
5. **Costo:** unos 20 s por informe con los datos ya en caché, más 3,3 min de video sin GPU. La primera validación con ICESat-2/GEDI de un sitio nuevo toma 20-30 min (solo validación, no va en cada informe).

## 2. Criterios de aceptación (spec, sección 8)

Salida de `poc/check_acceptance.py`: **PASS 6 · FAIL 1 · MANUAL 2** sobre los 8 pilotos. El criterio 6 se cierra con la evaluación de 24 sitios (`AUDITORIA/validacion_regla_sitios.md`), que el script de aceptación no lee todavía.

| # | Criterio | Estado | Evidencia |
|---|---|---|---|
| 1 | Reproducible de punta a punta | **PASS** | 8/8 hashes iguales entre las dos corridas; PDF, GLB, visor y video presentes; sin errores de consola del visor |
| 2 | Cero errores silenciosos de CRS | **PASS** | 27 pruebas con nombre; ningún rechazo produce informe |
| 3 | Desacuerdo GLO-30 vs AW3D30 | **PASS** | cifra y mapa en los 8 sitios (tabla 3) |
| 4 | Comparación con ICESat-2/GEDI | **PASS** | 303 a 729 huellas por sitio (tabla 3) |
| 5 | Veredicto Tier 2 | **PASS** (no promovido) | 2 de 8 sitios bajan ≥ 10 %; la spec cuenta "no promovido y documentado" como cumplido |
| 6 | La regla A/B/C discrimina | **PASS con 24 sitios** (el script con 8 pilotos sigue en FAIL) | sello de confianza del dato: A 1,98 m (8), B 5,40 m (9), C 7,58 m (7), Spearman 0,73; nivel final no discrimina (ver sección 4) |
| 7 | Informe legible por no expertos | MANUAL | 8 informes listos; 0 lectores |
| 8 | Licencias | MANUAL | falta leer los términos de JAXA y la revisión del abogado |
| 9 | Tiempo y cómputo | **PASS** | sección 5 |

## 3. Tabla por piloto

**Lote, sellos y factores** (regla v0.3.0, dosel medido con ESA WorldCover 10 m)

| Piloto | Lote (m²) | Píxeles | Pendiente | Dosel | Sellos: lote / dato | Nivel | Lo fija |
|---|---|---|---|---|---|---|---|
| rm01 Ñuñoa | 800 | 1 | no concl. | 100 % | C / B | C | píxeles, rango |
| rm02 Maipú | 15.000 | 16 | 3,1 % | 58 % | C / B | C | rango (3,0 m) |
| rm03 Peñalolén | 10.000 | 9 | 15,6 % | 98 % | B / B | B | píxeles, dosel |
| rm04 Lo Barnechea | 60.000 | 72 | 21,1 % | 49 % | A / B | B | pendiente, dosel |
| rm05 Puente Alto | 18.000 | 20 | 19,2 % | 6 % | B / A | B | píxeles |
| rm06 Huechuraba | 1.000 | 1 | no concl. | 0 % | C / B | C | píxeles, rango |
| rm07 Colina | 80.000 | 90 | 8,5 % | 27 % | A / B | B | dosel |
| rm08 Pirque | 70.000 | 72 | 52,0 % | 79 % | A / C | C | pendiente |

**Error medido (metros)**

| Piloto | AW3D30 − GLO-30: RMS / media | Huellas ICESat-2/GEDI | GLO-30: sesgo / RMSE | AW3D30: RMSE | Tier 2: RMSE crudo → corregido |
|---|---|---|---|---|---|
| rm01 Ñuñoa | 4,78 / 3,17 | 579 | 2,13 / 3,15 | 5,75 | 3,20 → 2,28 (−29 %) |
| rm02 Maipú | 4,07 / 3,98 | 690 | 0,65 / 2,52 | 4,68 | 2,58 → 2,50 (−3 %) |
| rm03 Peñalolén | 1,33 / 1,21 | 379 | 0,01 / 2,73 | 3,23 | 3,04 → 3,69 (+21 %) |
| rm04 Lo Barnechea | 0,79 / 0,50 | 520 | 1,67 / 10,59 | 10,51 | 14,30 → 14,18 (−1 %) |
| rm05 Puente Alto | 1,72 / 1,45 | 303 | 1,97 / 5,04 | 6,03 | 5,21 → 4,80 (−8 %) |
| rm06 Huechuraba | 2,24 / 1,91 | 579 | 1,85 / 4,49 | 5,74 | 3,81 → 3,66 (−4 %) |
| rm07 Colina | 3,09 / 3,02 | 642 | −0,85 / 6,68 | 6,90 | 8,09 → 8,19 (+1 %) |
| rm08 Pirque | 4,99 / 4,49 | 729 | 4,19 / 8,67 | 10,50 | 8,14 → 6,70 (−18 %) |

- **GLO-30 es mejor que AW3D30 en los 8 sitios** (o igual en Lo Barnechea). Se mantiene como dato base.
- **La diferencia media AW3D30 − GLO-30 es el sesgo propio de AW3D30**, no un error de GLO-30: por eso el factor de desacuerdo ya no la cuenta.
- **ICESat-2/GEDI exagera el error en ladera.** En Lo Barnechea da 10,6 m, mientras Pléiades 5 m da 3,5 m en la misma ventana. Las huellas de GEDI miden 25 m y tienen error de posición; en pendiente fuerte eso se convierte en metros de altura. La cifra de ICESat-2/GEDI en ladera es un techo, no el error real.

## 4. La regla A/B/C con 24 sitios

**Lo que se hizo.** Se separó el nivel en dos sellos: «medibilidad del lote» (píxeles, rango de elevación) y «confianza del dato» (pendiente, desacuerdo, dosel o construido, vacíos, incertidumbre propia). El nivel final sigue siendo el peor de los dos. El criterio se evalúa ahora con el segundo sello. Además se agregó la cobertura ESA WorldCover 10 m (CC BY 4.0).

**Qué mostró el dosel.** Se contrastó con las 4.421 huellas ICESat-2/GEDI de los 8 sitios (`AUDITORIA/dosel_vs_error_huellas.md`, `poc/tools/analisis_dosel_vs_error.py`):

| Clase bajo la huella | Error en terreno plano (<15 %) | Error en cerro (≥15 %) |
|---|---|---|
| Arbolado | 3,0 m | 8,3 m |
| Construido | 4,1 m | 6,7 m |
| Otro | 2,5 m | 9,3 m |

Árboles y edificios suman 0,5 a 1,6 m de error solo en terreno plano; en cerro no suman. La pendiente pesa mucho más. Por eso el dosel puede bajar el nivel a B pero no a C (`worst_level` en `confidence_rules.json`): los cortes originales de la spec (más de 50 % = C) mandaban a revisión humana lotes que no tienen más error.

**Cómo se probó.** Con 8 sitios no se podía decidir (A y C tenían un sitio cada uno). Se agregaron 16 lotes de prueba RM elegidos solo por pendiente y dosel (6 llanos, 4 intermedios, 6 de cerro; `poc/tools/elegir_sitios_validacion.py`) y la regla de evaluación se escribió antes de correrlos: error medio no decreciente A ≤ B ≤ C, al menos 3 sitios por nivel y correlación de rangos ≥ 0,5. No se tocó ningún corte de la regla.

**Resultado (24 sitios, error contra ICESat-2/GEDI):**

| Sello | Sitios A / B / C | Error medio A / B / C (m) | Mediana A / B / C (m) | Correlación | ¿Cumple la regla? |
|---|---|---|---|---|---|
| Confianza del dato | 8 / 9 / 7 | 1,98 / 5,40 / 7,58 | 1,71 / 3,15 / 6,91 | 0,73 | **Sí** |
| Medibilidad del lote | 13 / 4 / 7 | 6,81 / 3,08 / 2,38 | 6,68 / 2,52 / 2,03 | −0,51 | No |
| Nivel final | 2 / 8 / 14 | 1,04 / 5,72 / 4,98 | 1,04 / 3,89 / 4,21 | 0,17 | No |

Detalle por sitio en `AUDITORIA/validacion_regla_sitios.md`.

**Qué significa.**
- El **sello de confianza del dato** funciona: cuanto peor es el sello, más se equivoca el dato. En la práctica sigue a la pendiente (los 7 sitios C tienen 31 a 59 % de pendiente media; los 8 A, 0,6 a 19 %).
- El **sello de medibilidad del lote** mide otra cosa (si el lote es más grande que un píxel de 30 m), no el error del dato. Un lote llano y chico sale C pero se equivoca poco (Pudahuel 1,05 m, Buin 2,03 m); por eso no ordena el error. No es un fallo: es una pregunta distinta.
- El **nivel final** mezcla ambas preguntas y por eso deja de ordenar el error: 14 de 24 sitios caen en C, la mayoría por lote chico, no por dato malo. Antes de mostrarlo a clientes hay que mostrar los dos sellos por separado o redefinir el nivel final; es una decisión de producto, no se ajustó aquí.
- **B es el nivel menos fiable:** incluye a v10 San José de Maipo (13,9 m) y rm04 Lo Barnechea (10,6 m), los dos errores más altos de los 24 sitios, y también a lotes con 2 m. B significa «revisar», no «error intermedio».

**Límites de esta prueba.** Los sitios se eligieron por pendiente y dosel, que son justo lo que usa el sello del dato; sirve para probar que la pendiente anticipa el error, no para estimar qué fracción de lotes reales cae en cada nivel. El error es el de las huellas en 1 km alrededor, no el del lote, y en cerro las huellas de GEDI (25 m) exageran el error: las cifras de C son un techo. Son 24 sitios de una sola región.

## 5. Tiempo y cómputo

Máquina: Intel de 12 hilos, 15,7 GB RAM, Windows 11, video con WebGL por software.

- **Informe con datos en caché:** 16 a 27 s (GLB 1-2 s, PDF 0,4-1,1 s; el resto es red).
- **Primera corrida de un sitio:** 19 a 30 min, casi todo leyendo granos de GEDI (memoria pico ~8 GB). Es solo validación: un informe comercial no la necesita.
- **Video:** 160 a 225 s por sitio (3,3 min en promedio). Domina el costo.
- **Límite externo:** OpenTopography (AW3D30) permite 50 llamadas al día; la caché compartida evita repetirlas.
- Costo en dinero: cero.

## 6. Puerta de salida, ítem por ítem

| Ítem | Estado | Por qué |
|---|---|---|
| 1. Flujo reproducible de punta a punta | **Cumplida** | 8/8 hashes iguales, ahora con AW3D30 e ICESat-2/GEDI |
| 2. Mejora medible o límites documentados | **Cumplida** (límites documentados) | Tier 2 no mejora; el error de GLO-30 está medido por sitio y por clase de pendiente |
| 3. Insumos con licencia comercial confirmada | **NO** | términos de JAXA sin leer; dos fórmulas de atribución de GLO-30; sin abogado |
| 4. Informe y vitrina que se puedan mostrar | **NO** | texto legal en borrador, 0 lectores, y el nivel final A/B/C mezcla dos preguntas (el sello del dato sí está validado) |

## 7. Siguientes pasos, en orden

1. **Decidir cómo se muestra el nivel** (decisión de producto, ver sección 4): dos sellos por separado, o un nivel final que no mezcle lote chico con dato malo. Después, hacer que `check_acceptance.py` lea la evaluación de 24 sitios.
2. **Lectura de los términos de JAXA y revisión del abogado** (incluye ahora la atribución de ESA WorldCover).
3. **Prueba con 3-5 lectores no expertos**.
4. **Enviar las solicitudes de dato fino de la RM**.

## 8. Límites que siguen

- **Lotes urbanos chicos (800-1.000 m²):** el informe no da pendiente ni orientación, porque un píxel de 30 m no alcanza. Es el caso típico del cliente inmobiliario y hoy no tiene una respuesta útil.
- **Horizonte solar:** solo usa la ventana de trabajo. En lotes chicos los rayos miden 19 a 69 m y no capturan cerros cercanos.
- **Sin textura satelital:** el visor pinta el sombreado del propio DEM.
- **Sección 12 del PDF:** dice "enlace no publicado", porque el PDF se genera antes de publicar el visor.
- **Es DSM:** incluye edificios y árboles; no es terreno desnudo. Ahora el informe mide cuánto del lote es arbolado o construido (ESA WorldCover, año 2021), pero el modelo sigue sin sacarlos.
- **El agente (Claude Agent SDK) no se ha probado con el modelo real:** su costo en tokens no está medido.
- **Pléiades y SPOT son solo referencia interna** (no comerciales): no aparecen en ningún entregable.
