# Phase 1 — Especificación experimental de gestión dinámica del Stop Loss

Estado: especificación para revisión humana; sin autorización de implementación, acceso a nuevos períodos, backtesting o despliegue. Etiqueta de cualquier futuro resultado: **FORWARD_PAPER / NON_EXECUTING**.

Este experimento busca mejorar rentabilidad neta ajustada por riesgo sin degradar materialmente drawdown, costos ni oportunidades de Take Profit. No cambia el candidato C congelado, LIVE-0A, LIVE-0B, sus autorizaciones ni posiciones existentes. La política C de este documento es una variante de salida, no una nueva interpretación del candidato C histórico.

Los parámetros de las tres políticas y sus reglas temporales reflejan las decisiones aprobadas. Los umbrales estadísticos/económicos y períodos propuestos aquí deberán aprobarse y preregistrarse antes de observar resultados comparativos. No se han ejecutado experimentos para elegirlos.

## 1. Contrato común y notación

Las tres políticas conservan señales EMA9/21 sobre 4h completadas, ATR14 inicial, entrada precomprometida, prioridad BTCUSDT → ETHUSDT → ADAUSDT, un slot, riesgo planeado 1%, límite 3x, cantidad, costos y reglas de símbolos de la referencia autorizada. Conservan D = S + 14 minutos y E = S + 15 minutos para la aceptación/entrada inicial; ninguna salida dinámica permite reaceptar señales vencidas. Contextos 1D/1H/15m siguen siendo descriptivos.

Notación por operación:

- d = +1 para LONG y −1 para SHORT.
- P: precio efectivo de entrada, incluido el slippage original; Q: cantidad ejecutada.
- S0: stop inicial ya redondeado; TP0: target inicial ya redondeado.
- r0 = |P − S0| > 0, en unidades de precio; R0 = Q × r0, en USDT.
- C_t: cierre de una vela 4h completada en t; avance a cierre z_t = d × (C_t − P) / r0.
- F_e: fee de entrada realmente registrada; f_x: tasa de fee de salida; s: fracción de slippage adverso de salida.

R0 es riesgo geométrico inicial, no pérdida máxima garantizada ni riesgo neto de fees/gaps. Nunca se recalcula R al mover el stop. P, Q, S0, TP0 y R0 permanecen inmutables. Todos los cálculos financieros usan Decimal y el contexto aritmético versionado del motor; no se comparan umbrales con float.

La referencia actual en `paper/settlement.py` usa fee de entrada/salida 0,0005 y slippage 2 bps (s = 0,0002). El futuro manifiesto debe enlazar estos valores y sus identidades, sin cambiarlos. No se añade funding implícitamente: su ausencia en la referencia debe declararse como limitación económica, no introducirse solamente en una variante.

La protección inicial sigue siendo stop 2×ATR y target 4×ATR, con redondeo histórico. Se separan `initial_stop` y `active_stop`; jamás se sobrescribe S0 ni se modifica TP0.

## 2. Las tres políticas

### A — Baseline fijo

S_active = S0 durante toda la operación. TP = TP0. Las actualizaciones de salida están desactivadas. Debe reproducir la referencia congelada en decisiones, fills, cantidades, costos y resultados; no basta comparar rentabilidad agregada.

### B — Break-even neto y protección escalonada

Solo cierres 4h posteriores a la entrada pueden activar escalones. Se comparan inclusivamente con +1R y +1,5R. Una excursión intrabar que no se conserva al cierre no activa nada.

El P&L neto de una salida a precio efectivo X es:

`N(X) = d × Q × (X − P) − F_e − f_x × Q × X`.

El fill de break-even neto, antes de redondear el stop, satisface N(X_BE) = 0:

- LONG: `X_BE = (P + F_e/Q) / (1 − f_x)`.
- SHORT: `X_BE = (P − F_e/Q) / (1 + f_x)`.

El stop de referencia correspondiente es `S_BE = X_BE/(1−s)` para LONG y `S_BE = X_BE/(1+s)` para SHORT. La fee de entrada ya incluye el efecto de P; no se añade nuevamente el slippage de entrada.

Al cierre con z_t ≥ 1, el candidato es S_BE. Al cierre con z_t ≥ 1,5 también se calcula:

- `X_half = P + d × 0,5 × r0`.
- `S_half = X_half/(1−s)` LONG; `S_half = X_half/(1+s)` SHORT.

Este segundo escalón busca +0,5R **bruto después del slippage modelado y antes de fees**. No promete +0,5R neto. Se elige el stop más protector entre el vigente, BE y segundo escalón. Si BE resulta más protector por costos/ticks, prevalece BE.

Los hitos alcanzados quedan registrados y no se desactivan por retrocesos. Un salto directo a ≥1,5R produce una sola propuesta con el stop más protector, no dos activaciones artificiales. Un candidato inválido no cambia el stop: registra rechazo y puede volver a evaluarse en otro cierre 4h con identidad nueva; repetir el mismo cierre no genera otro intento. El hito alcanzado no equivale a una protección activa.

BE solo garantiza N ≥ 0 bajo el fill sin gap previsto, tasas y slippage modelados. Un gap adverso puede producir pérdida neta.

### C — Trailing 2×ATR14 de 4h, activado en +1R

Activación permanente en el primer cierre 4h posterior a la entrada con z_t ≥ 1. No incorpora el escalón BE de B.

Se usan extremos de **cierres 4h**, no máximos/mínimos intrabar:

- LONG: `H_t = max(P, cierres 4h posteriores a la entrada hasta t)`; candidato `H_t − 2×ATR_t`.
- SHORT: `L_t = min(P, cierres 4h posteriores a la entrada hasta t)`; candidato `L_t + 2×ATR_t`.

Se actualiza en cada cierre 4h desde la activación. El stop solo se estrecha; ATR creciente nunca lo afloja. Una caída de ATR puede estrecharlo sin un nuevo extremo. No se impone BE implícito.

ATR_t reproduce exactamente `research/features.py::_atr`: últimas 100 velas 4h completadas, primera como ancla; 99 true ranges; promedio de los primeros 14 TR como semilla y recurrencia `(13×ATR + TR)/14` para los restantes, Decimal con precisión 50. Se recalcula sobre esa ventana para equivalencia: una recurrencia perpetua desde otro origen no es sustituto equivalente. No hay vela en formación, optimización de longitud/multiplicador ni datos 1h/15m para activar trailing.

Parámetros cerrados: longitud 14, ventana 100, multiplicador 2, activación +1R, extremos de cierres desde entrada, frecuencia 4h. Una alternativa de extremos high/low sería otro experimento, no una decisión de implementación.

## 3. Redondeo, validación y ejecución

Para stops ajustados, LONG redondea hacia arriba al tick; SHORT hacia abajo. Esto evita perder la protección financiera propuesta por redondeo. No cambia el redondeo histórico del stop inicial, ni redondea nuevamente los fills con slippage.

Después de redondear, exigir:

1. Decimal finito y positivo, tick válido, límites inclusivos de precio habilitados del RuleSnapshot.
2. LONG: stop nuevo ≥ stop activo; SHORT: ≤ stop activo.
3. LONG: stop nuevo < TP0 y < C_t; SHORT: stop nuevo > TP0 y > C_t.
4. Posición todavía abierta y propiedad inequívoca del slot; evidencia completa y ordenada.

Igualdad con stop vigente es NO_CHANGE. Si cualquier otra validación falla, conservar stop vigente y registrar motivo; no clamp, no mover TP, no redimensionar, no cierre sintético al cierre 4h. Un gap posterior que cruce el stop válido se procesa normalmente.

Las reglas de ejecución 15m se conservan: gap adverso sobre SL usa OPEN con slippage adverso; gap favorable sobre TP conserva referencia TP sin regalar mejora; dual-touch SL+TP en una vela sin orden intrabar conocido resuelve STOP. Slippage queda incorporado en fills y no se deduce otra vez como costo separado. Se comprueba paridad con las funciones originales antes de cualquier prueba económica.

## 4. Causalidad, latencia y datos tardíos

Una vela 15m ocupa [o, o+15m). En la secuencia lógica:

1. Activar únicamente actualizaciones ya confirmadas cuya apertura efectiva sea o.
2. Resolver OPEN/gaps y después high/low contra las protecciones vigentes para esa vela.
3. Si la posición cerró, no calcular nuevas protecciones.
4. Tras completar la última vela del bloque 4h, calcular el candidato con el cierre/ATR disponibles.

Nunca se aplica el stop recién calculado al rango de la vela que lo produjo.

**Histórico idealizado:** una decisión del cierre t solo puede activarse en `t + 15m`. Es una hipótesis explícita de disponibilidad, no evidencia de que habría estado operativa en forward.

**Forward o replay con disponibilidad real:** sea p el instante registrado de confirmación posterior al commit que conserva el candidato y su evidencia. Definir:

`effective_open = max(t + 15m, siguiente apertura 15m estrictamente posterior a p)`.

No redondear p hacia atrás. Si p coincide con una apertura, se usa la siguiente. La confirmación debe estar persistida antes de effective_open; si no puede probarse, esa apertura no queda protegida por el nuevo stop. Se registra nueva elegibilidad posterior con evidencia append-only, nunca una activación retroactiva. Recuperación sin confirmación duradera conserva la protección anterior.

La implementación deberá demostrar esta frontera con un protocolo de propuesta/confirmación de disponibilidad; un timestamp tomado antes del commit no prueba disponibilidad. La precisión/incertidumbre temporal debe usar el contrato de reloj autorizado, sin inventar garantías físicas de fsync o del sistema operativo.

Durante Modern Standby no existe protección ejecutada por un exchange en este modelo. Al volver, se liquidan/reconcilian cronológicamente las velas pendientes con el stop que era efectivamente elegible en cada apertura. Un stop descubierto tarde puede quedar pendiente para una apertura futura, pero no proteger retrospectivamente la excursión perdida. Si el recorrido anterior ya cerró la operación, no se genera actualización para ella.

No saltar velas faltantes ni usar extremos/ATR futuros. El presupuesto puede pausar el recorrido conservando cursor, stop y eventos; el siguiente proceso continúa igual. Un append de LIVE-0A posterior al snapshot B6 no se incorpora hasta la próxima invocación.

Se publican por separado: resultados idealizados y resultados con disponibilidad real. No se mezclan sus operaciones ni se etiqueta una protección reconstruida como realmente disponible. La aceptación inicial conserva ONLINE/DELAYED, D y E originales en ambos casos; esta latencia de salida no extiende la ventana de entrada.

## 5. Persistencia, replay y compatibilidad

Propuesta de extensión futura, no cambio autorizado ahora:

- Namespace/run experimental separado; política A/B/C, versión y fingerprint en manifiesto inmutable.
- Eventos append-only de propuesta, confirmación de disponibilidad, activación, rechazo/no-change y cierre. IDs deterministas por run/posición/política/boundary/tipo; polls repetidos no duplican eventos.
- Cada cambio conserva stop anterior/propuesto/redondeado, P/Q/S0/TP0/R0, hitos, extremo de cierres, ATR y ventana/hash, evidencia fuente/receipt, tiempos de cálculo/persistencia/elegibilidad, RuleSnapshot, código/política y motivo.
- Proyección de stop activo, pending y cursor se actualiza atómicamente con su evento y reconciliación. Crash/rollback no deja stop efectivo sin evento ni cursor adelantado. Conflicto de mismo ID con contenido distinto bloquea.
- Cierre consume exactamente una protección vigente y una secuencia; no puede activar pendientes después de CLOSED. Eventos de salidas alimentan el accounting existente una sola vez.
- Replay despacha por versión registrada. Eventos históricos mantienen su interpretación sin necesitar una autorización de ejecución actual. La validación histórica no concede autoridad para operar.

`paper/settlement.py` verifica actualmente protecciones originales al reconstruir posiciones. No se puede mutar su stop silenciosamente: hará falta un estado experimental explícito y un adaptador de protección versionado. Journal/reconciliación deberán probar la cadena desde S0 hasta el stop efectivo. La rama baseline seguirá por el camino histórico.

No se incorpora esta gestión a la ADA existente ni al run actual. Una eventual prueba forward requerirá aprobación separada, run/política y frontera prospectiva propios. No se reescriben fingerprints, activation ni transiciones existentes.

## 6. Dos comparaciones obligatorias

**Entradas comunes:** lista congelada de entradas legales de referencia, mismo P/Q/R0/TP0, condiciones fuente y costos para A/B/C. Se evalúan contrafactuales de salida aunque se superpongan. No se presenta esa colección como portfolio ejecutable. Permite diferencias pareadas de P&L, giveback y TP sin confundirlas con selección de entradas.

**Portfolio completo:** tres recorridos independientes con un solo slot y misma equity inicial. Cada uno conserva riesgo 1% sobre su propia equity realizada y reglas de disponibilidad originales. Diferencias futuras de cantidades y señales bloqueadas por exposición son consecuencia de las salidas, no una nueva fórmula de sizing. Comparar además retorno/drawdown a exposición y tiempo en mercado; reportar señales perdidas por ocupación.

Mismo universo, fechas, warm-up, calidad de datos y convención terminal. No se sustituye el portfolio congelado por el simulador genérico de un símbolo ni su cierre END_OF_DATA.

Al corte: posiciones abiertas se valoran netas de liquidación hipotética, se informan como censuradas y no se inventa un trade cerrado. Métricas por trade usan cohortes con salidas observadas para las tres políticas; informar exclusiones/censura. Una censura >5% de entradas comunes impide conclusión confirmatoria. El valor terminal no se registra en el ledger real.

## 7. Métricas matemáticas preregistradas

Para cada cierre 15m, construir equity marcada neta de liquidación hipotética `V_t`: equity realizada más P&L abierto menos fees aún no contabilizadas y costo de salida al precio de referencia con slippage. No descontar fees ya cargadas. Para posiciones planas, V_t es equity realizada. Esta serie es analítica y no altera disponibilidad de capital.

`H_t = max(V_0, V_u para u ≤ t)`; `DD_t = (H_t − V_t)/H_t`; `MDD = max DD_t`; `R_net = V_T/V_0 − 1`.

| Prioridad | Métrica y definición |
|---|---|
| 1 | Rentabilidad ajustada por drawdown `U = R_net/MDD`, sobre idéntico horizonte. Si MDD = 0, U es no estimable para selección; no se asigna infinito. No anualizar períodos cortos para mejorar apariencia. |
| 2 | Expectancy neta `EN = media(N_i/R0_i)` de trades cerrados; reportar además media USDT, mediana y distribución. |
| 3 | MDD de V_t, no solamente drawdown de equity realizada. Reportar también duración bajo máximo. |
| 4 | `PF = suma(max(N_i,0))/suma(max(−N_i,0))`; sin pérdidas el cociente es no estimable para promoción. |
| 5 | `G_i = (max(0, N_i, max_t L_i(t)) − N_i)/R0_i`, con L_i(t) valor neto de liquidación a cierres 15m durante la operación. Giveback medio/mediano/p90; no confundir con MFE intrabar. |
| 6 | `TP_rate = número de salidas TP / número de trades cerrados`; comparar también llegada a TP por entrada común y cantidad censurada. |

N_i contiene fees de entrada/salida y slippage en fills. Reportar costos separados: fees/R0, shortfall por slippage/R0, turnover, costo total/equity inicial, duración y exposición. El shortfall explica N_i; no se vuelve a restar.

En entradas comunes añadir `TP_lost = proporción de entradas donde A alcanza TP y variante no`; `TP_gained` inversa, y P&L neto pareado. En portfolio estas tasas no sustituyen la diferencia de oportunidades por slot.

## 8. Criterios de aceptación propuestos antes de resultados

Estos son límites de tolerancia económica para aprobación, no estimaciones ni umbrales ajustados a ADA. Márgenes absolutos pequeños evitan que una ratio atractiva o menor giveback oculte rentabilidad/costos/TP sustancialmente peores.

Para promover B o C frente a A deben pasar **todos** los siguientes, en confirmación y en ambos modos de disponibilidad cuando haya evidencia suficiente:

| Criterio | Umbral propuesto |
|---|---|
| Validez | Cero violaciones causales, duplicados, discrepancias de accounting/replay o alteraciones históricas. |
| Rentabilidad | R_net y EN positivos; A con R_net positivo para declarar superioridad relativa según U. Si A no es rentable: revisión separada, no ganador automático. |
| Mejora principal | U_variante ≥ 1,10 × U_A y límite inferior ajustado del intervalo de `U_variante − U_A` > 0 en portfolio. |
| Retorno no inferior | Límite inferior ajustado de `R_net_variante − R_net_A` ≥ −0,02 (2 puntos porcentuales sobre el horizonte fijado). |
| Expectancy | Límite inferior ajustado de EN_variante − EN_A ≥ −0,05R, en entradas comunes y portfolio. |
| Drawdown | Límite superior ajustado de MDD_variante − MDD_A ≤ min(0,02; 0,10×MDD_A). Además MDD observado ≤20%. |
| Profit Factor | PF observado ≥1,10 y diferencia observada PF_variante − PF_A ≥−0,10. |
| Giveback | Giveback medio observado no aumenta; publicar intervalo de la diferencia y p90, incluso si TP mejora. |
| TP | Límite inferior ajustado de diferencia TP_rate ≥−5 puntos porcentuales y límite superior ajustado de TP_lost ≤10% en entradas comunes. |
| Costos | Límite superior ajustado del incremento de costo medio por entrada común ≤0,02R; turnover/costo total de portfolio obligatoriamente reportados. |

La mejora mínima del 10% exige relevancia más allá de ruido; −0,05R y −2pp limitan sacrificio económico; límites TP limitan oportunidades perdidas; 20% DD es un techo de investigación propuesto, no garantía ni autorización de riesgo live. Todos pueden rechazarse en esta revisión, pero no modificarse tras observar resultados sin declarar un experimento nuevo.

Muestra mínima confirmatoria: ≥200 trades cerrados por política, ≥30 trades perdedores por política para PF, ≥20 trades por cada una de las seis combinaciones símbolo/dirección, y ≥12 meses calendario. Entradas comunes ≥200 completas. No garantizan potencia: intervalos demasiado amplios implican INCONCLUSO aunque se cumplan cantidades.

Robustez: tablas de cada símbolo/dirección y trimestre predefinido, costos, DD y EN. Con muestra suficiente, ninguna combinación puede presentar deterioro observado de EN >0,20R; al menos 4 de 6 combinaciones y 3 de 4 trimestres deben tener diferencia de EN ≥0. Si falta cobertura, no se afirma robustez general. No seleccionar retrospectivamente subuniversos ganadores. Resultados con y sin la operación más rentable se publican como sensibilidad, sin eliminarla del resultado principal.

## 9. Incertidumbre, multiplicidad y decisión

Plan propuesto: 10.000 réplicas de bootstrap por bloques temporales comunes de 28 días, semilla preregistrada 20261008. Comparaciones usan los mismos bloques para A/B/C y agrupan simultáneamente símbolos para no asumir independencia de señales correlacionadas. Portfolio usa incrementos de equity neta normalizados respecto de equity inicial y reconstruye la curva remuestreada para U/MDD; no se afirma que cada curva bootstrap sea un nuevo portfolio causal ejecutado. Entradas comunes se agrupan por fecha de entrada y preservan los pares.

Dos comparaciones primarias (B−A, C−A): intervalos unilaterales 97,5% por candidato, equivalentes al control Bonferroni de 5% familiar. Todos los gates de cada candidato son conjuntivos: no se elige el gate favorable. Métricas descriptivas llevan intervalos bilaterales 95% sin atribuirles significancia confirmatoria. Sensibilidades de bloques 14/56 días se publican; si cambian la conclusión favorable, resultado inconcluso. Dependencia prolongada, pocas agrupaciones o censura excesiva impiden promoción; bootstrap no corrige sesgo de datos.

Decisiones posibles: NO SUPERIOR, INCONCLUSO o CANDIDATA A CONFIRMACIÓN/PROMOCIÓN. Ningún resultado autoriza despliegue. Si ambas variantes pasan, no declarar superioridad B vs C sin comparación preregistrada adicional: preferencia operativa provisional por B por menor dependencia de indicadores, con ambas reportadas. Prohibido elegir únicamente por U, PF, TP o una operación llamativa.

## 10. Períodos propuestos, todavía no habilitados

1. **Validación técnica:** fixtures sintéticos y históricos ya autorizados, sin métricas para selección. Verificar inventario/permisos antes de cualquier lectura futura.
2. **Investigación exploratoria propuesta:** 2024-01-01T00:00Z a 2025-01-01T00:00Z, intervalo semiabierto. Solo si el inventario y autorización histórica permiten reapertura explícita. 2024 ya investigado no se llama holdout. No buscar nuevos parámetros; medir tres contratos cerrados.
3. **Validación histórica secundaria propuesta:** 2025-01-01T00:00Z a 2026-01-01T00:00Z, únicamente mediante autorización separada y revisión de exposición previa. No afirmar que está intacto. El material 2026 ya observado, incluida ADA, sirve para diagnóstico causal y nunca como confirmación independiente ni para ajustar umbrales.
4. **Confirmación preferida:** doce meses prospectivos completos desde una futura frontera 4h aprobada después de congelar implementación, parámetros y plan; fin = inicio +12 meses calendario. No se elige esa fecha ahora. La confirmación debe registrar disponibilidad real. Si no alcanza tamaño/precisión, conclusión INCONCLUSA; cualquier extensión requiere protocolo nuevo aprobado, no prolongación oportunista hasta ganar.

Warm-up anterior al comienzo (mínimo requerido por contrato de indicadores, incluida ventana 600 cuando corresponda) es HISTORY_ONLY y no entra en métricas. No se abre una posición antes del inicio y se la cuenta como nueva. Si los períodos sugeridos son incompatibles con gobernanza de datos, se reemplazan antes de ver resultados y mediante aprobación; este documento no habilita ninguno.

## 11. Plan de pruebas y aceptación técnica futura

- Paridad A contra golden vectors y replay histórico: entradas, protecciones, fills, fees, P&L y journal semántico idénticos.
- LONG/SHORT: umbrales justo debajo/en/encima de 1R y 1,5R, salto directo, latching, retroceso, no-change y no relajación.
- BE neto algebraico con ambas fees/slippage, segundo escalón bruto, ticks gruesos, límites de precio, valores inválidos, BE más estricto que +0,5R y ausencia de doble costo.
- ATR: semilla/ventana exactas, aumento/disminución, extremos desde entrada, ausencia de high/low futuro, gaps de datos y 4h incompletas.
- Temporalidad: decisión t nunca afecta vela anterior; t+15m exacto; persistencia antes/en/después de apertura; confirmación demorada; Standby; receipts tardíos; cierre anterior a disponibilidad; datos conflictivos; nueva fuente tras captura B6.
- Ejecución: gap SL/TP, dual-touch STOP, stop cruzado en primera apertura efectiva, TP inmutable, no redondeo de fill, ninguna operación retroactiva.
- Crash/reinicio en cada fase de propuesta/confirmación/activación/cierre; idempotencia, discrepancias bloquean, journal/proyección/cursor atómicos, missing candle nunca saltada, un slot y accounting exactamente una vez.
- Aislamiento: pruebas no acceden a live, red ni tareas; fixtures solo autorizados; ningún evento histórico cambia de interpretación; autorización histórica no permite ejecutar.
- Estadística: fórmulas verificadas con ejemplos manuales, PF sin pérdidas/U sin DD no estimables, censura, bloques pareados, multiplicidad y gates reproducibles.

Aceptación técnica requiere todos los casos, suite offline completa y auditoría de diff/identidades antes de datos económicos. La terminación abrupta del sistema durante un commit y la disponibilidad física del host seguirán siendo limitaciones explícitas si no pueden ensayarse determinísticamente.

## 12. Arquitectura y costo estimado

Futura implementación aislada desde un commit aprobado, sin compartir carpetas runtime ni DB de producción. Módulo puro de política recibe posición inmutable, snapshot 4h y política; devuelve candidato/motivo. Planificador temporal convierte candidato en actualización elegible; ejecutor 15m consume solo stop ya autorizado/efectivo. Journal/replay experimental separado integra accounting sin reescribir histórico.

Con un slot, B añade O(1) por cierre4h; C añade como máximo 99 TR/reducciones por cierre4h si no reutiliza el ATR ya verificado y un extremo incremental. No requiere nuevas peticiones, otros procesos Git ni escaneos completos de fuente. El bucle 15m sigue O(n) sobre las velas pendientes; la mayor carga probable es persistencia/replay, no aritmética. Esto es estimación, no benchmark medido.

Objetivos offline propuestos sobre fixtures representativos, fresh-process y mismo equipo: mantener exactamente un Git y un snapshot/verificación fuente por invocación; p95 ≤15s, máximo ≤30s; incremento p95 frente al baseline medido ≤max(1s, 10% del p95 baseline). Medir también journal largo/backlog y eventos por posición. No subir cadence/timeout para ocultar degradación. PT1M/PT2M/IgnoreNew no se modifican.

Fases posteriores requieren autorización individual: implementación/fixtures aislados → validación técnica → estudio exploratorio autorizado → confirmación preregistrada → revisión independiente. No hay paso automático hacia el run existente.

## 13. Cierre y decisiones pendientes

Contrato funcional propuesto completamente especificado: no quedan parámetros de trailing, referencia de activación, redondeo, TP o latencia para elegir después de ver resultados.

Pendiente de aprobación humana antes de ejecución: márgenes y muestra de secciones 8–9; autorización e inventario de períodos; fecha futura de confirmación; commit base/run experimental y presupuesto de implementación. Si se desea cambiar cualquiera, se versiona la especificación antes de acceder a resultados.

Esta Phase 1 solo produce este documento. No certifica el estado operativo actual ni modifica la posición ADA, el candidato congelado o los runs existentes. No se ejecutaron consumidores, backtests ni pruebas, ni se habilitaron períodos de datos para redactarlo.
