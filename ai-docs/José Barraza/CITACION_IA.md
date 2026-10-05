# Citación de uso de IA — E1 (backend + frontend)

> Documento de trabajo. Declara la asistencia de IA recibida durante la
> elaboración de las tareas E1. **No incluir en PRs de features**
> (mover a `ai-docs/` según convención del equipo o mantener fuera de Git).

- **Estudiante:** José Barraza (`sudo-giuseppe`)
- **Asistente:** Muse Spark, vía OpenCode (agente de codificación con
  ejecución de comandos, lectura/escritura de archivos y navegación web).
- **Período:** 22–28 sep 2026 (Fase 2 E1).
- **Repositorios:** `coniverav/E0-Arquisis-local` (backend) y
  `VicenteIgnacioSotoGonzalez/Arquisis-G10-Frontend` (frontend).

Regla de trabajo aplicada: el estudiante implementa y decide
(commits, push, PRs, dashboard Auth0, pruebas manuales en navegador);
la IA investiga, propone planes, implementa piezas puntuales a pedido
explícito y verifica con ejecución (tests, build, lint).

## Backend — `E0-Arquisis-local`

### E1-13 — Tenant Auth0
- IA: plan paso a paso de creación (tenant, API con audience
  `https://arquisis-e1-api/`, app M2M, token `client_credentials`) y
  diagnóstico de errores (`access_denied`: `client_id`/`client_secret`
  intercambiados, app M2M sin autorizar contra el API, `AUTH_ISSUER`
  con ruta JWKS en vez de dominio raíz).
- Estudiante: todo el trabajo en dashboard Auth0 + obtención del token.
- Verificación conjunta: `verify_jwt` real contra token real →
  `OK: {sub, iss, aud}` (Paso 6).

### E1-57 — Verificación JWT vía JWKS (rama `feat/e1-57-jwt-jwk`, PR #13)
- Estudiante: esqueleto inicial (`verify_jwt`, tests placeholder).
- IA: revisión con bloqueadores; implementación de
  `master/app/auth.py` (`fetch_jwks` con caché 600 s, `verify_jwt`
  RS256 + issuer/audience/exp, 401/503, fail-closed) y
  `master/tests/test_auth.py` (RSA 2048 local, 4 casos);
  reposición de comentarios del estudiante eliminados por error.
- Verificación: `4 passed`.

### E1-55 — `POST /negotiations` (consolidado en PR #13)
- Estudiante: `NegotiationCreate` en `schemas.py`, registro del router
  en `main.py`, commit `681f284`.
- IA: guía de diseño; corrección `cycleid` → `cycleId`;
  creación de `master/app/routers/negotiations.py` (201/200/404,
  idempotencia por `idpk`, `TODO(E1-43)`) y de
  `master/tests/test_negotiations.py` (8 casos); smoke end-to-end
  201/200/404/401/401/422 contra Postgres real.
- Verificación: suite completa `62 passed`.

### Sincronización Git (ambos repos)
- IA: diagnóstico y resolución de rebases con conflicto
  (`config.py`, `requirements.txt`, `docker-compose.yml`: conservar
  ambos lados), detección de marcadores de conflicto commiteados en
  `.env.example` + fix con valores verificados del tenant,
  rebase sobre `develop`, `push --force-with-lease` a pedido,
  gestión de ramas apiladas.
- Estudiante: todos los `pull/push`, decisiones de consolidación
  (E1-55 dentro de PR #13) y merges.

## Frontend — `Arquisis-G10-Frontend`

### E1-37 — Login/logout + token en SPA (commit `f8cd632`, mergeado PR #4)
- Estudiante: SDK instalado, `Auth0Provider` inicial, `.env.example`,
  app SPA + usuario en dashboard, pruebas en navegador.
- IA: guía Auth0 (incl. autorización de la app SPA contra el API,
  error `not authorized to access resource server`); corrección de
  `main.jsx` (doble `<App/>`, import faltante); implementación de
  `AuthButton.jsx`, `services/auth.js` (token-getter) e inyección
  `Authorization` en `apiFetch`; verificación `build` + `lint`.

### E1-60 → E1-63 (rama `feat/e1-60-63-frontend-views`, 4 commits ES)
- IA (a pedido explícito por pieza): `ConnectivityView.jsx` + cableado
  en `App.jsx` (E1-60); `NegotiationForm.jsx` + `createNegotiation` +
  cableado con error 401 inline + login (E1-61); badges de estado
  final, dirección y columna de vencimiento en `CycleDetail` (E1-62);
  `AnomaliesView.jsx` con filtros + cableado (E1-63); commits por
  tarea; reescritura de mensajes a español verificando árboles
  idénticos; corrección de 2 errores `react-hooks/set-state-in-effect`.
- Estudiante: `getConnectivity()` (Paso 1 E1-60), matrices de prueba
  manual, decisiones de push/PR/squash.
- Verificación IA: `npm run build` + `npm run lint` verdes tras cada pieza.

## Guías entregadas (sin código)

E1-55 (diseño), E1-13→E1-57 (Paso 6), E1-60–63 (por tarea con forma
exacta de endpoints), E1-64–67 (Cloud/ECR/EC2, archivo borrado tras uso),
matrices de prueba manual, mensajes de commit y textos de PR.
