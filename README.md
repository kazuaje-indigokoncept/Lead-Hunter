# LeadHunter

Herramienta local para generar leads B2B desde Google Maps y otras fuentes, con panel web, campañas por cliente, puntaje de calidad y exportación a Excel/CSV.

## Qué hace

| Fuente | Cómo | Qué obtiene |
|---|---|---|
| **Google Maps** | Places API (New), API oficial | Nombre, categoría, dirección, teléfono, web, rating, reseñas, enlace a Maps |
| **Google Maps sin API** | Navegador Chromium con Playwright sobre las fichas públicas | Nombre, categoría, dirección, teléfono, web, rating y reseñas cuando están visibles |
| **Sitio web del negocio** | Visita portada y hasta 4 páginas adicionales (configurable 0–8); comprueba robots.txt en cada página y redirección | Correos, todos los teléfonos encontrados, WhatsApp, Instagram, Facebook, TikTok, LinkedIn, procedencia por dato y auditoría básica |
| **LinkedIn público** | Detecta enlaces en la web del negocio; consulta páginas públicas de empresa si permiten acceso | Título y contactos publicados accesibles; registra bloqueos, acceso restringido o necesidad de sesión |
| **Instagram público sin API** | HTML público de perfiles encontrados o añadidos; comprueba robots.txt | Biografía, seguidores completos y contactos publicados cuando el HTML los expone; registra bloqueos |
| **Instagram con API** | Graph API de Meta, *Business Discovery* | Seguidores y bio de cuentas profesionales |
| **Directorio RUES** (Colombia) | Datos Abiertos (datos.gov.co), API oficial | Establecimientos activos por cámara de comercio, código CIIU y fecha de matrícula. Se pueden cruzar con Google Maps para sacar el contacto |

Cada lead recibe un **score de 0 a 100** (correo 25, WhatsApp 20, teléfono 15, web 10, redes 10, rating ≥ 4 10, ≥ 20 reseñas 10). Puedes marcar su estado (nuevo, contactado, interesado, descartado) y agregar notas.

## Oportunidades, prioridades y etiquetas

Al crear una campaña, selecciona **Diseño web**, **SEO local**, **Gestión de redes** o **Prospección general**. En la lista puedes cambiar de perfil sin repetir el scraping; la prioridad se recalcula y puede cambiar el orden de los leads.

- **Diseño web:** web no encontrada en la ficha de Maps, respuestas HTTP de error, HTTP sin HTTPS, formulario de contacto o etiqueta de adaptación móvil no encontrados en las páginas revisadas.
- **SEO local:** título o descripción SEO no encontrados, menos de 20 reseñas y valoración inferior a 4.
- **Gestión de redes:** Instagram y Facebook no enlazados en las páginas revisadas.
- **General:** prioriza contactos disponibles, actividad observada y presencia digital.

La prioridad combina contacto (correo 20, teléfono 15, enlace WhatsApp publicado 10), actividad (al menos 20 reseñas: 10) y encaje con el servicio (hasta 45). El panel muestra el desglose exacto de cada lead. Es una heurística, no una predicción de compra. El score anterior se conserva como **Datos disponibles** y no equivale a calidad verificada.

Las etiquetas visibles usan cuatro estados: **encontrado** (verde), **no encontrado en las fuentes revisadas** (ámbar), **sin revisar** (gris) y **WhatsApp posible** (amarillo, deducido de un teléfono). La ausencia de un enlace no demuestra ausencia de una web o red social. Puedes pulsar una etiqueta para filtrar, filtrar por oportunidad y prioridad mínima, o cambiar el servicio para comparar resultados. Exportar Excel/CSV mantiene los mismos filtros y añade etiquetas, oportunidades, prioridades y evidencias.

En la ficha del lead se muestra la fuente, URL de origen, fecha UTC de observación, confianza y tipo de evidencia de cada dato. **Cobertura de procedencia** mide qué proporción de los valores disponibles tienen evidencia guardada; no mide exactitud, identidad ni entregabilidad. Los correos se etiquetan como detectados, sin validar entrega. No se envían correos de prueba ni mensajes. Los enlaces de WhatsApp publicados tampoco validan que una cuenta esté activa.

La auditoría básica lee HTML público, enlaces de correo/teléfono/WhatsApp, texto telefónico con contexto y datos estructurados JSON-LD. Busca redes sociales en enlaces y `sameAs`. No ejecuta JavaScript de la web, por lo que puede omitir contenido que solo se renderiza en el navegador. Un timeout, bloqueo o respuesta 403/429 no se interpreta como web averiada. Cada página y redirección comprueba robots.txt; no se accede a destinos privados o locales.

Puedes corregir o añadir la web y los enlaces de Instagram y LinkedIn desde la ficha. Guarda el cambio y usa **Volver a buscar contactos y oportunidades** o **Consultar página pública de empresa**. Se preservan contactos anteriores y se añade su nueva procedencia. En campañas antiguas, una nueva ejecución revisa webs que todavía no tienen auditoría. La base SQLite se actualiza automáticamente con columnas adicionales sin eliminar registros; los datos antiguos conservan su estado de procedencia desconocida.

## Empresas y responsables

La ficha permite editar el nombre comercial de la empresa. La vista **Empresas y responsables** reúne registros de distintas campañas en una ficha de empresa. Conserva los registros originales, direcciones, redes, oportunidades y procedencia; no elimina las filas de campaña ni convierte toda coincidencia en una sede confirmada. Agrupa por el mismo identificador de origen, una URL exacta de empresa de LinkedIn, nombre normalizado más dominio exacto o nombre/dirección/ciudad exactos. No agrupa solo por nombre, teléfono o dominio compartido. Ante señales contradictorias mantiene empresas separadas. Las asociaciones existentes permanecen estables: puedes corregirlas con el ID de empresa desde la ficha de cada registro o separar un registro en otra empresa. La migración añade las tablas y asociaciones al iniciar sin borrar datos existentes.

Cada empresa contiene **personas de contacto** separadas de los correos y teléfonos generales. Se guardan nombre, cargo, perfil profesional, contactos publicados de esa persona, evidencia de relación, fecha y último estado de consulta. El extractor lee datos públicos `Person`, `jobTitle`, `worksFor` y enlaces `/in/` desde las páginas visitadas. Los enlaces sin evidencia laboral quedan como candidatos pendientes. Los vínculos publicados siguen pendientes de comprobar vigencia; el usuario puede confirmar, dejar pendiente o descartar. No se supone que un cargo implica poder de compra y no se inventan nombres, cargos o direcciones de correo.

La prioridad del contacto se ajusta al servicio: responsables digitales para diseño web, dirección de marketing para SEO/redes y dueños/gerencia para decisiones generales. Los perfiles pendientes o descartados reciben prioridad cero. Es encaje heurístico por cargo, no una predicción de compra. La prioridad comercial de la empresa que muestra la tabla es el máximo de sus registros, no una suma de sucursales ni una nueva medida de verificación.

**Buscar responsables de esta empresa** revisa hasta cinco webs únicas (portada + hasta ocho páginas de contacto/equipo), consulta hasta tres páginas de empresa y hasta quince perfiles de personas encontrados o añadidos. En campaña se usan los candidatos descubiertos por la fase web. Un perfil personal solo aporta datos cuando su HTML público identifica ese perfil y publica una organización coincidente; `alumniOf` o la presencia del nombre de empresa en texto libre no demuestran empleo actual. Los errores y bloqueos conservan contactos previos. Las decisiones humanas prevalecen ante nuevas extracciones. Los contactos de demostración aparecen marcados como DEMO.

Si las fuentes no enlazan personas o LinkedIn no permite acceder, la ficha ofrece una búsqueda específica por empresa y cargos para localizar perfiles manualmente y añadirlos con su fuente. Esta búsqueda se abre en el navegador del usuario; no existe un rastreador automático de resultados de Google o un buscador masivo de empleados dentro de LinkedIn.

## Alcance de LinkedIn

Seleccionar **LinkedIn público** activa también la búsqueda de enlaces en las webs de los negocios encontrados por Maps/RUES. Se consultan URLs `/company/` y perfiles `/in/` vinculados a una empresa cuando son accesibles sin sesión y permitidos por robots.txt. Los perfiles de persona se gestionan en la ficha de empresa. No se usan cuentas, cookies de sesión, proxies, captchas ni endpoints internos. LinkedIn puede bloquear por robots.txt, sesión, verificación o límite de acceso; se registra el motivo y HTTP 429 detiene la fase. La consulta pública sigue sujeta a las condiciones de LinkedIn ([acuerdo de usuario](https://www.linkedin.com/legal/user-agreement)).

LinkedIn puede bloquear todo acceso automatizado, exigir sesión o limitar información, incluso cuando un enlace es público. En esos casos se conserva el enlace y se muestra el estado correspondiente; no se promete extracción de contactos. Sus condiciones restringen el scraping automatizado: [User Agreement](https://www.linkedin.com/legal/user-agreement) y [software prohibido](https://www.linkedin.com/help/linkedin/answer/a1341387/prohibition-of-scraping-software). El usuario debe comprobar qué uso está autorizado antes de activar consultas de esa fuente.

## Interfaz con Tailwind CSS

La interfaz usa Tailwind CSS 4.3, con navegación lateral, menú móvil, filtros rápidos y avanzados, etiquetas de contacto y fichas de empresa. `static/app.css` ya está compilado y se sirve desde la propia app: no necesitas Node.js para ejecutarla ni un CDN para los estilos.

Para modificar el diseño, instala Node.js y ejecuta `npm ci` y `npm run build:css` dentro de la carpeta del proyecto. El archivo fuente es `ui/styles.css`; Tailwind detecta también las clases de `static/index.html`. Durante el desarrollo puedes usar `npm run watch:css`. Recompila después de editar clases o estilos y recarga el navegador. Configuración y dependencias están en `package.json` y `package-lock.json`.

## Instalación (Windows)

1. Instala Python 3.10 o superior desde python.org y marca **Add Python to PATH**.
2. Descomprime la carpeta y haz doble clic en **`iniciar.bat`**.
   La primera vez instala dependencias (1–2 min). Luego abre `http://127.0.0.1:8000`.

Mac/Linux: `./iniciar.sh`

El iniciador de Windows comprueba que Python funcione, carga `.env` y espera a que el servidor responda antes de abrir el navegador. Si Chromium no se descarga, el panel puede arrancar; Google Maps sin API requerirá completar esa instalación. La ventana conserva los errores al terminar.

Si no abre, ejecuta **`diagnostico.bat`** y revisa `logs/diagnostico.txt`. Los errores de arranque se guardan en `logs/arranque_error.txt`. Si el puerto está ocupado, ejecuta `iniciar.bat -Puerto 8001` desde la consola. La contraseña inicial aparece en la ventana al crear la base por primera vez; también puedes configurar `ADMIN_USER` y `ADMIN_PASSWORD` en `.env` antes del primer arranque. Para actualizar, conserva `data/` y `.env`.

La app arranca en **modo demostración** (datos ficticios) para que la pruebes sin gastar nada.

## Probar con Docker en tu PC (Windows)

1. Instala **Docker Desktop** (docker.com/products/docker-desktop) y ábrelo hasta que diga *Engine running*.
2. Descomprime `leadhunter.zip` y haz doble clic en **`iniciar-docker.bat`**.
3. Elige una contraseña para `admin`. Se abrirá `http://localhost:8000`.

Manual (PowerShell, dentro de la carpeta): copia `.env.example` como `.env`, pon tu contraseña y ejecuta
`docker compose -f docker-compose.local.yml up -d --build`.
Apagar: `docker compose -f docker-compose.local.yml down` · Registros: `docker compose -f docker-compose.local.yml logs -f`

Esta versión local no usa Caddy ni HTTPS y solo se abre desde tu PC. En el servidor se usa `docker-compose.yml`.

## Subir a un servidor (Hostinger VPS u otro Ubuntu)

Necesitas un **VPS** (en Hostinger: planes KVM). El hosting web compartido no sirve porque no mantiene apps Python encendidas.

1. En hPanel → VPS, instala el sistema **Ubuntu 24.04** (o la plantilla “Ubuntu + Docker”). Anota la IP y la contraseña root.
2. Desde tu PC (PowerShell en Windows), sube el zip:
   `scp leadhunter.zip root@TU_IP:/root/`
3. Entra al servidor (`ssh root@TU_IP` o la *Terminal del navegador* de hPanel) y ejecuta:
   ```
   apt update && apt install -y unzip
   unzip leadhunter.zip && cd leadhunter
   bash deploy/instalar.sh
   ```
4. El script instala Docker, genera la contraseña del usuario `admin`, abre los puertos 80/443 y arranca todo. Al final muestra la dirección, del tipo `https://72-60-10-20.sslip.io` (HTTPS gratis sin comprar dominio, gracias a sslip.io).
5. Entra, cambia la contraseña (menú lateral → Contraseña) y crea los usuarios de tu equipo en **Ajustes**.

Si en hPanel tienes activado el firewall del VPS, permite los puertos 80 y 443.

**Comandos útiles** (dentro de la carpeta `leadhunter`):
- Ver registros: `docker compose logs -f app`
- Reiniciar: `docker compose restart`
- Actualizar a una versión nueva: reemplaza los archivos (sin borrar `data/` ni `.env`) y ejecuta `docker compose up -d --build`
- Copia de seguridad: descarga `data/leads.db` (contiene leads, usuarios y API keys)

**Con dominio propio:** crea un registro DNS tipo A (ej. `leads.tuagencia.com` → IP del VPS), cambia `SITE_ADDRESS` en `.env` y ejecuta `docker compose up -d`.

## Verificación de contactos y nivel de confianza

**Verificación gratuita** (sin enviar correos ni llamar). Se hace sola al final de cada campaña o tanda, al volver a revisar una web, con *Verificar ahora* en la pestaña Evidencia del lead, y para toda la base con **✓ Verificar contactos** (vuelve a comprobar lo que tenga más de 30 días).
- **Correos:** formato, nombres de archivo capturados por error (`logo@2x.png`), correos de ejemplo o de servicios externos, dominios temporales o desechables, errores de escritura (`gmial.com`) y si el dominio tiene servidor de correo (registros DNS MX). No confirma que el buzón concreto exista; para eso hace falta un servicio de pago.
- **Teléfonos:** válidos para su país y tipo (móvil, fijo…), con la librería de Google `phonenumbers`. La región por defecto es la de la campaña. No confirma que la línea esté activa.
- Los correos que fallan la verificación **no se incluyen en las exportaciones** de la base.

**Nivel de confianza** (0–100; Alta ≥ 70, Media ≥ 40, Baja < 40) de cada negocio en la base, con el desglose visible:
- +15 si el mismo teléfono aparece en dos fuentes distintas (por ejemplo Maps y la web), +10 si está en RUES, +10 si la web fue revisada y es accesible, +5 si aparece en varias campañas.
- +20 si hay un correo con dominio que recibe correo (+5 si es del mismo dominio que la web), +15 si el teléfono es válido; −10 si los correos o el teléfono fallan.
- +20 / +12 / +6 si el dato se observó en los últimos 30 / 90 / 180 días.
- +5 si Google Maps lo marca abierto; −15 si está cerrado temporalmente; −50 si está cerrado permanentemente.
- En la base puedes filtrar y ordenar por confianza, y registrar entregas solo con confianza media o alta. La exportación incluye el nivel, el puntaje y la verificación del teléfono.

## Base de datos general (solo administradores)

La sección **Base de datos** reúne en un solo lugar todos los negocios encontrados por las campañas de todo el equipo, sin duplicados. Es el activo comercial del negocio: se alimenta sola y se puede segmentar, exportar y vender.

**Cómo se llena**
- Cada lead real de cualquier campaña (del administrador o de los usuarios) se une automáticamente a su registro en la base al encontrarse, enriquecerse o editarse. Los datos de **modo demostración no entran**.
- Un mismo negocio se reconoce por coincidencias exactas: la misma ficha de Google Maps, la misma matrícula RUES, el mismo dominio web con el mismo nombre, la misma página de LinkedIn, el mismo nombre y dirección, o el mismo teléfono con el mismo nombre. Una cadena con el mismo dominio pero otro nombre queda en registros separados.
- El registro combina todas sus fuentes: todos los correos y teléfonos, y el dato más reciente de web, redes, valoración y estado. Muestra de qué campañas y usuarios viene, cuándo se vio por primera vez y cuándo se observó el último dato.
- Borrar una campaña no borra sus negocios de la base.
- **Corregir datos** (nombre, categoría, ciudad, dirección) los protege de futuras actualizaciones automáticas. **Fusionar** une dos registros duplicados. **Resincronizar** vuelve a recorrer todas las campañas.

**Comercialización**
- **Filtros de segmento:** texto, ciudad, categoría, con correo/teléfono/WhatsApp/web/Instagram, sin web, antigüedad del dato, completitud mínima, nunca entregados o no entregados a un cliente.
- **Exportar / entregar:** Excel o CSV, con orden y cantidad máxima. *Registrar entrega* guarda qué registros recibió cada cliente, con precio y notas, y por defecto no repite registros que ese cliente ya tenga. Desde **Entregas** se vuelve a descargar o se anula.
- **Datos personales separados:** por defecto solo se exportan datos del negocio y los correos genéricos (info@, ventas@, contacto@…). Los correos con nombre de persona y las personas de contacto solo se incluyen si se marca *Incluir datos personales*.
- **Lista de exclusión:** para quien pide no figurar. Excluir un registro borra sus datos de contacto de la base, bloquea su dominio y teléfonos para campañas futuras y nunca se exporta. También se pueden añadir exclusiones por dominio, teléfono, correo o nombre.

> Comercializar datos personales suele requerir la autorización de sus titulares (por ejemplo, Ley 1581 de 2012 en Colombia y LOPDP en Ecuador). Valida con un abogado qué datos puedes vender y con qué condiciones.

## Pantalla de inicio

Al entrar, la app abre **Inicio**: un resumen con leads totales, nuevos hoy, últimos 7 días (comparado con la semana anterior), porcentaje con correo, campañas activas y perfiles consultados hoy. Incluye la gráfica de leads por día (30 días), las próximas tandas programadas con su cupo del día, el embudo por estado y las categorías principales. Cada usuario ve sus datos; el administrador ve todo el equipo, el crecimiento de la base de datos general y la actividad por usuario. Las gráficas tienen tabla de datos y se pueden recorrer con el teclado.

## Campañas programadas por franja horaria

Para reducir el riesgo de bloqueos por scraping, una campaña puede ejecutarse dentro de una **franja horaria diaria** con un **máximo de perfiles por día**. Es la opción predeterminada al crear una campaña (sección **Ejecución**).

- **Franja horaria:** una barra de 24 horas con dos controles para elegir el inicio y el fin (tramos de 30 minutos, mínimo 1 hora). Las tandas se activan a **horas aleatorias** dentro de la franja, distintas cada día, y nunca fuera de ella.
- **Perfiles máximos por día:** una barra con semáforo. Verde hasta el 60 % del tope del servidor, amarillo hasta el 90 % y rojo al llegar al máximo. Al alcanzar el cupo, la campaña espera a la franja del día siguiente.
- Una **búsqueda** es una palabra clave en una ubicación (3 palabras × 2 ubicaciones = 6 búsquedas). Cada tanda hace una búsqueda y revisa la web, LinkedIn e Instagram solo de los negocios encontrados en ella. Si el cupo corta una búsqueda, se repite completa en la siguiente franja (los repetidos se unifican).
- **Tope del servidor** (Ajustes): máximo de perfiles al día entre todas las campañas (300 por defecto). **Zona horaria** (Ajustes): la hora en la que se interpretan las franjas (Bogotá por defecto).
- Solo se ejecuta una tanda programada a la vez en todo el servidor. Tras un error se espera al menos una hora; tras 3 errores seguidos la campaña queda en **Error**.
- En la campaña, la tarjeta de estado muestra la próxima tanda, los perfiles de hoy con el semáforo, un punto por búsqueda (hecha, siguiente, cortada por el cupo, pendiente) y una línea de 24 horas con la franja, las tandas de hoy y la hora actual.
- Acción principal según el estado (**Activar**, **Pausar** o **Reanudar programación**), **⏱ Programación** para cambiar franja y cupo, y el menú **⋯** con *Ejecutar una tanda ahora*, exportar a Excel o CSV y eliminar.
- Si el servidor se reinicia, la tanda interrumpida se vuelve a programar dentro de la franja.

## Leads: tabla y detalle

- Toda la fila abre el detalle del negocio; los filtros están solo en la barra superior.
- El **estado** (nuevo, contactado, interesado, descartado) se cambia directamente en la tabla o en la cabecera del detalle.
- El detalle se divide en pestañas: **Contacto** (datos, etiquetas y corrección de enlaces), **Oportunidades** (prioridad, oportunidades y empresa), **Evidencia** (calidad, revisiones y fuente de cada dato) y **Notas**.

## Usuarios y privacidad de datos

Cada usuario ve solo sus propias campañas, leads, empresas y contactos; no puede abrirlos, modificarlos ni exportarlos aunque conozca su número. El administrador ve todo, y en la barra lateral aparece de quién es cada campaña. Si el mismo negocio aparece en campañas de dos usuarios, cada uno tiene su propia ficha de empresa.

Los administradores gestionan el equipo en la sección **Usuarios** del menú lateral:

- **Nuevo usuario:** propone una contraseña aleatoria y copia los datos de acceso al portapapeles para compartirlos.
- **Rol:** Usuario (solo lo suyo) o Administrador (todo, más usuarios y ajustes). Siempre debe quedar al menos un administrador activo.
- **Desactivar / Activar:** bloquea el acceso al instante y cierra sus sesiones, sin borrar datos. Es la opción recomendada cuando alguien deja el equipo.
- **Contraseña:** asigna una nueva y cierra sus sesiones abiertas.
- **Renombrar**, **Ver campañas** (con acceso directo a cada una) y **Eliminar** (sus campañas y leads pasan al administrador que lo elimina).
- La tabla muestra estado, sesiones abiertas, campañas, leads y último acceso de cada persona.
- **Si el administrador olvida su contraseña:** define la variable `RESET_ADMIN_PASSWORD` en el servidor, reinicia, entra y borra la variable.
- Las claves de API (Ajustes) son compartidas y solo las cambia un administrador.
- Los datos creados antes de separar por usuario pasan automáticamente al primer administrador.

## Configurar las APIs (Ajustes)

### Google Maps sin API

1. Ejecuta `iniciar.bat` (Windows), `iniciar.sh` (Mac/Linux) o reconstruye la imagen Docker. Los iniciadores instalan Playwright y Chromium automáticamente; la primera descarga tarda unos minutos.
2. En Ajustes, desactiva **Modo demostración** para usar datos reales. No necesitas guardar ninguna clave de Google.
3. Crea una campaña con **Google Maps** y el modo **Sin API (navegador)**. Es el modo predeterminado para campañas nuevas; las campañas antiguas mantienen Places API.
4. Define palabras clave, ubicaciones y un máximo de fichas por consulta (40 por defecto, entre 1 y 120). Mantén **Sitio web** seleccionado para buscar correos y redes en las webs encontradas.

El navegador visita fichas de forma secuencial, con una pausa de 2 segundos, y guarda cada resultado inmediatamente. Solo se ejecuta un navegador de Maps a la vez. Si pulsas Detener, se termina la operación de navegador actual antes de cerrar (una carga puede tardar hasta 45 segundos). Los leads guardados se conservan. Reiniciar la campaña repite las consultas y evita duplicados dentro de ella.

Google puede mostrar captchas, limitar el acceso o cambiar el diseño; ante un bloqueo, la campaña termina en estado **error** y deja el motivo en el registro. No se resuelven captchas ni se usan proxies o técnicas de evasión. No se garantiza cobertura completa ni que todos los campos estén disponibles. Los límites se aplican a fichas visitadas antes de filtrar por rating o descartar duplicados. El teléfono no confirma que el negocio tenga WhatsApp. El modo sin API no tiene cargos de Places, pero consume recursos del equipo/servidor. El cruce RUES con Maps sigue disponible únicamente en modo API.

Instalación manual:
```sh
python -m pip install -r requirements.txt
python -m playwright install chromium
```
En Linux, si faltan bibliotecas del sistema: `python -m playwright install --with-deps chromium`. Docker las incluye en la imagen. Los scripts de inicio guardan Chromium en `.browsers/`; si instalas manualmente, usa el mismo `PLAYWRIGHT_BROWSERS_PATH` o la ruta predeterminada de Playwright tanto al instalar como al ejecutar.

Referencias técnicas: [instalación de Playwright](https://playwright.dev/python/docs/library) y [navegadores](https://playwright.dev/python/docs/browsers).

### Google Maps con Places API (opcional)
1. Entra a console.cloud.google.com, crea un proyecto y activa la facturación.
2. Habilita **Places API (New)**.
3. Crea una API key en *Credenciales* y restríngela a Places API.
4. Pégala en Ajustes y desactiva el modo demostración.

Costos: los campos de teléfono, web y rating usan el SKU *Text Search Enterprise*. Cada llamada trae hasta 20 negocios; Google da un cupo gratis mensual por SKU. El panel muestra el costo máximo estimado antes de lanzar cada búsqueda. Pon una alerta de presupuesto en Google Cloud.

**Límite de 60 resultados por consulta:** divide por barrios/localidades y usa sinónimos (ej. “odontología”, “clínica dental”, “ortodoncia” × “Laureles”, “El Poblado”, “Belén”). Los duplicados se eliminan solos.

### Instagram público, sin API (opcional)

Selecciona **Instagram** y el modo **Público, sin API** al crear la campaña. Es el modo predeterminado del formulario nuevo; las campañas antiguas conservan el modo API. No requiere token de Meta ni cuenta conectada. Al seleccionar Instagram también se revisan las webs de los negocios para descubrir enlaces a sus perfiles; esta fase completa leads existentes, no busca negocios por ubicación dentro de Instagram.

Se consulta el HTML público del perfil respetando robots.txt, sin iniciar sesión, ejecutar JavaScript ni consultar endpoints privados. Cuando el HTML contiene datos del perfil, se guardan biografía, contador completo de seguidores (si está publicado), correos, teléfonos y enlaces explícitos de WhatsApp. Los contadores abreviados no se presentan como cifras exactas. Otros enlaces externos publicados quedan en la ficha para revisión manual. Cada dato conserva fuente, URL y fecha; la publicación no valida la identidad del negocio ni la entrega de los correos.

En la ficha del lead puedes añadir o corregir el perfil, **Guardar** y pulsar **Consultar perfil público sin API**. La revisión muestra estados de robots.txt, HTTP 404, acceso restringido, sesión/verificación o HTML sin datos suficientes. Los bloqueos no se interpretan como ausencia de contactos y no borran datos anteriores. El cambio de perfil sí limpia su biografía, seguidores y estado de revisión para evitar asociar métricas de otra cuenta. La consulta secuencial espera 2 segundos entre perfiles; HTTP 429 detiene la fase de Instagram y conserva los resultados.

Instagram puede exigir sesión o publicar solo una carcasa JavaScript: en esos casos la extracción no estará disponible. No se eluden bloqueos ni se usan cuentas, cookies de sesión, proxies o mecanismos de evasión. Meta exige permiso para la recopilación automatizada; la visibilidad pública no equivale a autorización ([información de Meta](https://about.fb.com/ltam/news/2021/04/como-combatimos-el-scraping/)).

### Instagram con API de Meta (opcional)
Requiere una cuenta de Instagram **profesional** propia vinculada a una página de Facebook, una app en developers.facebook.com y un token (Facebook Login) con `instagram_basic` y `pages_read_engagement`. Pega el token y el ID de tu cuenta IG en Ajustes. Límite aprox. 200 consultas/hora. Solo funciona con perfiles de empresa o creador.

### RUES / datos.gov.co (opcional)
Funciona sin clave. Para muchas consultas crea un *app token* gratis en datos.gov.co. En “Ubicaciones” escribe el nombre de la Cámara de Comercio (ej. “Medellín”, “Bogotá”, “Aburrá Sur”). Usa “Matriculados desde” para encontrar negocios recién abiertos.

## Límites honestos
- **Facebook:** Meta ya no permite buscar páginas por API; los Facebook se obtienen desde la web del negocio y Google Maps.
- **Directorios comerciales** (Páginas Amarillas, etc.) no tienen API; no se raspan para no violar sus términos. RUES cubre solo Colombia.
- Los correos extraídos de sitios web no están verificados. Antes de campañas de email masivas, valídalos (NeverBounce, ZeroBounce, o Apollo).

## Uso responsable (Colombia)
Los datos de contacto comercial públicos pueden usarse para prospección B2B, pero aplican la Ley 1581 de 2012 (Habeas Data) y, según el caso, la Ley 2300 de 2023 (horarios y canales de contacto): identifícate, ofrece una forma fácil de darse de baja, respeta horarios de contacto y no envíes mensajes masivos no solicitados por WhatsApp (también va contra las políticas de WhatsApp Business). Si trabajas en otros países, revisa su normativa (GDPR en Europa, CAN-SPAM en EE. UU.).

## Estructura
```
Dockerfile, docker-compose.yml, deploy/   despliegue en servidor (Caddy + HTTPS)
app/
  main.py            API + servidor del panel
  auth.py            usuarios, contraseñas y sesiones
  jobs.py            ejecución de campañas en segundo plano (y datos demo)
  db.py              SQLite (data/leads.db)
  companies.py       empresas, asociaciones y contactos profesionales
  sources/places.py  Google Places API
  sources/maps_web.py Google Maps en navegador sin API
  sources/rues.py    Datos Abiertos Colombia
  enrich/website.py  extracción de correos, WhatsApp y redes
  enrich/instagram.py Business Discovery de Meta
  enrich/instagram_public.py perfiles públicos sin API
  enrich/people.py    personas publicadas y evidencia de cargo/organización
static/index.html    panel web
```

Para agregar una fuente nueva, crea un módulo en `app/sources/` que devuelva leads con `dedupe_key`, `source`, `name`, etc., y añade una fase `phase_<nombre>` en `jobs.py`.

## Pruebas del modo sin API

Desde la carpeta del proyecto: `python -m unittest discover -s tests -v`.
Las pruebas de backend cubren normalización, creación de campañas sin clave, estimaciones, límites, deduplicación, enriquecimiento web, conservación de resultados ante bloqueo y parada, y compatibilidad del modo API. La navegación se simula en estas pruebas.

Para ejecutar también la extracción de DOM y detección de captchas con Chromium sobre páginas de prueba locales, instala el navegador y configura `MAPS_BROWSER_TESTS=1` (PowerShell: `$env:MAPS_BROWSER_TESTS='1'`; Bash: `export MAPS_BROWSER_TESTS=1`) antes de ejecutar el comando anterior.

Validación de esta entrega: 60 pruebas de backend aprobadas, incluidos agrupación entre campañas, separación manual de empresas, vinculación de contactos a su empresa, priorización de cargos, descarte de empleos anteriores y empresas homónimas, límites HTTP 429, Instagram público con respuestas simuladas, conservación de contactos ante bloqueo, compatibilidad de la API de Meta, perfiles, filtros, exportación, migración de datos, procedencia, extracción web y límites de LinkedIn. Se verificó la interfaz en el navegador integrado con datos de demostración: filtros, cambio de perfil, etiquetas, ficha, selector de Instagram sin API y revisión individual de Instagram en demostración. También se verificó la vista de empresas con registros de dos campañas, la búsqueda de responsables en demostración y la confirmación manual de un vínculo. Las 2 pruebas con Chromium para Maps siguen omitidas porque el entorno de desarrollo bloquea su proceso auxiliar. No se verificó scraping real de Google Maps/LinkedIn/Instagram ni se construyó la imagen Docker en este entorno. Las comprobaciones de fuentes externas requieren una prueba en el equipo donde se ejecute la app.
