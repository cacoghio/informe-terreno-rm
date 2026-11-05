# Error real de GLO-30 contra un DSM de referencia fino — 8 pilotos RM

**USO INTERNO — validación.** Pléiades 2016 y SPOT 2014 son CC BY 4.0 con la nota del portal "this data is not permitted for commercial use". **No entran** al informe PDF, al visor, al GLB, al video ni a `publicado/`.

- Cortafuegos en código: `poc/terreno/refdem.py` no importa módulos que generen entregables, y ninguno de ellos lo importa. Lo verifica `poc/tests/test_refdem.py`.
- Los rasters de referencia viven solo en `poc/cache/refdem/`.

Medición: 2026-10-31, `python -m terreno.refdem --site all --source all`.

## 1. Referencias

| Clave | Dataset | DOI | Época | Malla |
|---|---|---|---|---|
| `pleiades` | Survey of the San Ramon Fault, Santiago, with 2016 Pleiades imagery (OTDS.022021.32719.1) | 10.5069/G9CC0XWZ | 2016 | 5 m |
| `spot` | Survey over Santiago from 2014 SPOT imagery (OTDS.022021.32719.2) | 10.5069/G97M064K | 2014 | 10 m |

- Autores: Hussain (BGS) y Elliott (Univ. de Leeds).
- Las alturas son **elipsoidales WGS84**: el archivo no declara datum vertical, y la prueba empírica lo confirma (tabla de la sección 3).
- Se resta la ondulación N de EGM2008 (malla PROJ `us_nga_egm08_25`, unos 28 m) para comparar con GLO-30.
- **Pléiades es la referencia principal.** SPOT tiene un sesgo propio de −2 a −3 m (sección 4): contra SPOT valen NMAD, clases de pendiente y pendiente, pero no el sesgo.

## 2. Cobertura de los 8 pilotos

| Piloto | Comuna | Pléiades | SPOT |
|---|---|---|---|
| rm01_plano_urbano | Ñuñoa | fuera | sí |
| rm02_plano_periurbano | Maipú | fuera | sí |
| rm03_piedemonte_oriente | Peñalolén | sí | sí |
| rm04_piedemonte_alto | Lo Barnechea | sí | sí |
| rm05_piedemonte_sur | Puente Alto | sí | sí |
| rm06_cerro_isla_urbano | Huechuraba | fuera | sí |
| rm07_lomaje_norte | Colina | fuera | fuera |
| rm08_precordillera | Pirque | fuera | fuera |

Colina y Pirque se validan solo con ICESat-2/GEDI.

## 3. Resultados de GLO-30

- **Sesgo** = GLO-30 − referencia (DSM contra DSM, EGM2008), en metros.
- **ctx** = ventana del polígono ± 1500 m.
- **lote** = solo si hay 9 o más píxeles de 30 m. Ñuñoa (800 m²) y Huechuraba (1.000 m²) tienen 1 píxel, así que no hay cifras de lote.
- **Residuo datum** = mediana (nube − GLO-30) − N. Si es cercano a 0, las alturas son elipsoidales.

**Ventana de entorno (± 1500 m), en metros**

| Piloto | Ref. | n | Sesgo | RMSE | NMAD | Error pendiente (°) | Residuo datum |
|---|---|---|---|---|---|---|---|
| rm03 Peñalolén | Pléiades | 10092 | 0,81 | 1,69 | 1,23 | 0,8 | −0,91 |
| rm04 Lo Barnechea | Pléiades | 11058 | −0,28 | 3,53 | 1,76 | 1,6 | −0,30 |
| rm05 Puente Alto | Pléiades | 9658 | 0,61 | 2,26 | 1,32 | 0,9 | −0,74 |
| rm01 Ñuñoa | SPOT | 9950 | 1,33 | 3,91 | 1,40 | 2,0 | −1,88 |
| rm02 Maipú | SPOT | 8037 | 1,35 | 2,49 | 0,96 | 1,0 | −1,56 |
| rm03 Peñalolén | SPOT | 9815 | 2,39 | 2,91 | 1,46 | 0,8 | −2,37 |
| rm04 Lo Barnechea | SPOT | 11217 | 2,46 | 3,86 | 1,30 | 1,4 | −2,70 |
| rm05 Puente Alto | SPOT | 8985 | 2,53 | 3,31 | 1,49 | 0,9 | −2,38 |
| rm06 Huechuraba | SPOT | 8658 | 2,43 | 6,82 | 1,45 | 1,0 | −2,03 |

**Lote y RMSE por clase de pendiente, en metros** (Ñuñoa y Huechuraba sin cifras de lote: 1 píxel)

| Piloto | Ref. | n lote | Sesgo lote | RMSE lote | RMSE < 5 % | 5-15 % | 15-30 % | > 30 % |
|---|---|---|---|---|---|---|---|---|
| rm03 Peñalolén | Pléiades | 9 | 1,00 | 1,31 | 1,5 | 1,3 | 1,8 | 2,1 |
| rm04 Lo Barnechea | Pléiades | 72 | 1,12 | 1,45 | 1,9 | 3,1 | 4,2 | 2,8 |
| rm05 Puente Alto | Pléiades | 15 | −0,42 | 1,54 | 1,9 | 2,0 | 2,1 | 2,2 |
| rm01 Ñuñoa | SPOT | — | — | — | 2,6 | 4,1 | 6,5 | 9,6 |
| rm02 Maipú | SPOT | 16 | 1,52 | 1,66 | 2,0 | 2,4 | 3,3 | 4,2 |
| rm03 Peñalolén | SPOT | 9 | 1,99 | 2,14 | 1,9 | 2,4 | 3,2 | 3,9 |
| rm04 Lo Barnechea | SPOT | 71 | 2,95 | 3,06 | 3,0 | 3,5 | 4,7 | 4,3 |
| rm05 Puente Alto | SPOT | 20 | 2,53 | 3,66 | 2,1 | 2,6 | 3,1 | 3,1 |
| rm06 Huechuraba | SPOT | — | — | — | 1,9 | 2,3 | 3,0 | 3,0 |

AW3D30 no se midió en esta corrida porque OpenTopography llegó a su límite de 50 llamadas por día. Su comparación contra Pléiades de la ronda anterior (Lo Barnechea) daba RMSE 3,1 m, igual a GLO-30.

### Lectura

- **Contra Pléiades, GLO-30 tiene un RMSE de 1,7 a 3,5 m en el entorno y de 1,3 a 1,5 m en el lote.**
  - Sesgo entre −0,3 y +0,8 m.
  - NMAD de 1,2 a 1,8 m.
  - El error de pendiente es menor a 2 grados.
  - Es mejor que el orden de 3,5-4 m que suponía la spec.
- **El RMSE sube con la pendiente**, de 1,5-1,9 m bajo 5 % a 1,8-4,7 m entre 15 y 30 %. Sobre 30 % no empeora en el piedemonte (2,1-2,8 m contra Pléiades), pero en Ñuñoa sí empeora contra SPOT (9,6 m, en los pocos píxeles empinados del borde del cerro).
- **La cola gruesa (RMSE mucho mayor que NMAD) viene de edificios, árboles y quebradas:**
  - Huechuraba tiene un RMSE de entorno de 6,8 m, pero bajo 3 m en todas las clases de pendiente.
  - Los errores grandes están en los ~3.500 píxeles sin clase. Son bordes de huecos de SPOT en el cerro, donde SPOT cubre solo el 85 %.
- **Error relativo dentro del lote** (RMSE sin el sesgo): 0,7 a 1,5 m contra Pléiades y hasta 2,6 m contra SPOT. Es la base del nuevo corte de "rango de elevación" (sección 5).

## 4. Sesgo propio de SPOT

- En los tres sitios con ambas referencias, SPOT queda 1,6 a 2,7 m bajo Pléiades en la ventana de entorno (sesgo de GLO-30 contra SPOT menos sesgo contra Pléiades).
- El residuo de datum muestra lo mismo: −1,6 a −2,7 m con SPOT y −0,3 a −0,9 m con Pléiades.
- Coincide con el chequeo GEDI de la ronda anterior (SPOT 2,3 m bajo Pléiades).
- Conclusión: el sesgo contra SPOT es de la referencia, no de GLO-30.

## 5. Calibración de la regla A/B/C (2026-10-31)

Con estas cifras y las de ICESat-2/GEDI en los 8 pilotos:

1. **Desacuerdo GLO-30 vs AW3D30: se quita el desfase medio.**
   - La diferencia media AW3D30 − GLO-30 (+0,5 a +4,5 m) coincide, con un margen de 1,4 m, con el sesgo extra de AW3D30 frente a ICESat-2/GEDI en 7 sitios (Ñuñoa queda fuera por las huellas con nube).
   - Ese desfase no cambia la pendiente ni el relieve.
   - Ejemplo: en Colina el RMS era 3,09 m con media 3,02 m. Sin el desfase queda en 0,65 m, y ese factor pasa de B a A.
2. **Rango de elevación: cortes de 16/8 m a 8/4 m.**
   - El desnivel compite con el error relativo dentro del lote (0,7-2,6 m), no con el error absoluto (LE90 < 4 m).
   - B exige al menos 2 × 2 m, y A el doble de B.
   - El aviso "dentro del ruido" del informe usa el mismo corte de 4 m.
3. **Sin cambio:**
   - Píxeles: el lote chico sigue siendo C, porque con 1 píxel no hay cifra de lote posible.
   - Pendiente: los datos no muestran un quiebre claro sobre 20 o 30 %.
   - Vacíos y HEM.
4. **Dosel/construido sigue sin dato**, así que ningún sitio puede ser A. Falta una capa pública de cobertura; la candidata es ESA WorldCover 10 m, CC BY 4.0.

Todos los cortes siguen marcados como hipótesis (`poc/data/confidence_rules.json`, versión 0.2.0), porque son 5 lotes con referencia fina.

## 6. Salvedades

- **Épocas distintas:**
  - referencias de 2014 y 2016;
  - GLO-30 viene de TanDEM-X, 2011-2015;
  - las construcciones nuevas o los árboles talados entre épocas cuentan como "error".
- **La exactitud absoluta de la referencia no está declarada.** Son nubes fotogramétricas sin puntos de control publicados, así que las cifras son error relativo a una referencia imperfecta.
- **Es DSM contra DSM:** incluye edificios y vegetación y no mide el terreno desnudo.
- **Archivos de la ronda anterior:** los JSON y PNG de `AUDITORIA/` con nombres de sitios viejos (Lo Barnechea, Ñuñoa, Viña/Reñaca, Puerto Varas, Coquimbo) están obsoletos y pendientes de borrar.

## 7. Reproducir

```
cd poc
.venv/Scripts/python -m terreno.refdem --site all --source all
```

- Requiere las dos nubes en `poc/cache/refdem/` y `OPENTOPOGRAPHY_API_KEY` para AW3D30.
- Salida: `AUDITORIA/error_vs_referencia_fina_<sitio>_<fuente>.json` y `AUDITORIA/diff_<sitio>_<fuente>_glo30.png`.
- Tabla de este documento: `tabla_refdem.py` (script ad hoc).
