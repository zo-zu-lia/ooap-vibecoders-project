# 5. Error Handling Design

[← До вступу та змісту](../FileFlow_SRS.md)

## 7. REST API — OpenAPI / Swagger

### 7.1. Контракт та загальні правила

Повний машинозчитуваний контракт — **[`FileFlow_OpenAPI.yaml`](../FileFlow_OpenAPI.yaml)**. Його можна вставити у [Swagger Editor](https://editor.swagger.io/) або відкрити у Swagger UI. Специфікація містить **29 HTTP-операцій**, схеми запитів та відповідей, security scheme (session cookie), обов'язкові заголовки і типізовані відповіді з помилками. Адреса `https://files.example.corp/api/v1` — умовна.

- **Формат:** JSON UTF-8; типовий префікс `/api/v1`; дати — RFC 3339 / ISO 8601 UTC.
- **Авторизація:** `__Host-fileflow_session` cookie; якщо її немає або сесія недійсна — `401`.
- **CSRF:** кожен JSON-запит, що змінює стан (`POST`, `PUT`, `PATCH`, `DELETE`), передає `X-CSRF-Token`, отриманий через `GET /me`. Виняток — браузерні OIDC redirects, які мають власну перевірку `state`.
- **Ідемпотентність:** для створення upload-сесій, `complete`, відновлення версій, відновлення з кошика і створення share-link обов'язковий `Idempotency-Key` (UUID). Однаковий ключ із таким самим тілом дає ту саму збережену відповідь протягом 24 год; той самий ключ з іншим тілом — `409 IDEMPOTENCY_KEY_REUSED`.
- **Доступ:** персональні права перевіряються **на сервері під час кожної видачі signed URL**. Неавторизоване існування об'єкта може маскуватися відповіддю `404`.
- **Rate limit:** цільове значення 120 REST-запитів/хв на працівника (у production налаштовується); за перевищення — `429` та `Retry-After`. Прямі S3-запити мають окремі ліміти сховища.
- **URL download:** дійсний не більш ніж 60 с; multipart PUT URL — до 15 хв, прив'язаний до номера частини й методу.

### 7.2. Перелік endpoint-ів

| # | HTTP endpoint (після `/api/v1`) | Дія / успішний код | Основні помилки |
|---:|---|---|---|
| 1 | `GET /me` | Поточний користувач і CSRF-токен; `200` | `401`, `429` |
| 2 | `POST /logout` | Завершення сесії; `204` | `400`, `401`, `429` |
| 3 | `GET /workspaces` | Доступні простори; `200` | `401`, `429` |
| 4 | `GET /workspaces/{workspaceId}/items` | Вміст папки, cursor pagination; `200` | `400`, `403`, `404` |
| 5 | `POST /workspaces/{workspaceId}/folders` | Створити папку; `201` | `400`, `403`, `404`, `409` |
| 6 | `GET /workspaces/{workspaceId}/policy` | Прочитати квоту / retention; `200` | `403`, `404` |
| 7 | `PUT /workspaces/{workspaceId}/policy` | Оновити квоту / retention; `200` | `400`, `403`, `404`, `409` |
| 8 | `GET /workspaces/{workspaceId}/trash` | Переглянути кошик; `200` | `403`, `404` |
| 9 | `POST /folders/{folderId}/uploads` | Створити сесію нового файла; `201` | `400`, `403`, `404`, `409`, `422` |
| 10 | `POST /files/{fileId}/uploads` | Сесія нової версії; `201` | `400`, `403`, `404`, `409` |
| 11 | `GET /uploads/{uploadId}/parts/{partNumber}/url` | Підписаний PUT URL частини; `200` | `400`, `403`, `404`, `409`, `429` |
| 12 | `POST /uploads/{uploadId}/complete` | Завершити multipart і запланувати сканування; `202` | `400`, `409`, `422`, `429`, `503` |
| 13 | `GET /uploads/{uploadId}` | Стан `PROCESSING/READY/REJECTED/CONFLICT`; `200` | `403`, `404`, `429` |
| 14 | `GET /files/{fileId}` | Метадані, поточна й придатна версії; `200` | `403`, `404` |
| 15 | `GET /files/{fileId}/versions` | Історія незмінних версій; `200` | `403`, `404` |
| 16 | `POST /files/{fileId}/versions/{versionNo}/restore` | Відновлення версії як нової; `201` | `403`, `404`, `409`, `422` |
| 17 | `GET /files/{fileId}/download-link` | Signed GET URL останньої `CLEAN` версії; `200` | `403`, `404`, `409`, `422` |
| 18 | `DELETE /files/{fileId}` | Перенести в кошик; `204` | `403`, `404`, `409` |
| 19 | `POST /files/{fileId}/recover` | Повернути з кошика; `200` | `403`, `404`, `409` |
| 20 | `PATCH /files/{fileId}/legal-hold` | Установити/зняти hold; `200` | `403`, `404`, `409` |
| 21 | `GET /files/{fileId}/permissions` | Переглянути явні файлові правила; `200` | `403`, `404` |
| 22 | `PUT /files/{fileId}/permissions/{userId}` | Надати `VIEWER/EDITOR`; `200` | `400`, `403`, `404` |
| 23 | `DELETE /files/{fileId}/permissions/{userId}` | Відкликати пряме правило; `204` | `403`, `404` |
| 24 | `GET /files/{fileId}/share-links` | Перелік посилань (без секретних токенів); `200` | `403`, `404` |
| 25 | `POST /files/{fileId}/share-links` | Внутрішнє посилання для адресата; `201` | `400`, `403`, `404`, `422` |
| 26 | `DELETE /share-links/{shareLinkId}` | Відкликати посилання; `204` | `403`, `404` |
| 27 | `POST /share-links/resolve` | Розв'язати внутрішнє посилання; `200` | `400`, `403`, `404`, `422` |
| 28 | `GET /search` | Шукати дозволені файли за назвою; `200` | `400`, `403`, `429` |
| 29 | `GET /audit-events` | Переглянути аудит (security admin); `200` | `401`, `403`, `429` |

**Примітка:** усі endpoint-и також можуть повернути `500`; залежності зі сховищем — `503`. У YAML перелік кодів наведено **окремо для кожної операції**. Таблиця показує найхарактерніші з них, а не повний перелік.

### 7.3. Приклади контрактів

#### A. Ініціалізація нового файла

```http
POST /api/v1/folders/123e4567-e89b-42d3-a456-426614174000/uploads HTTP/1.1
Cookie: __Host-fileflow_session=<session-cookie>
X-CSRF-Token: <csrf-token>
Idempotency-Key: 00000000-0000-4000-8000-000000000101
Content-Type: application/json
```

```json
{
  "name": "requirements.pdf",
  "sizeBytes": 16777216,
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "contentType": "application/pdf"
}
```

`201 Created`:

```json
{
  "uploadId": "00000000-0000-4000-8000-000000000201",
  "status": "INITIATED",
  "partSizeBytes": 8388608,
  "partCount": 2,
  "expiresAt": "2026-10-12T09:00:00Z"
}
```

Далі клієнт отримує URL кожної частини через `GET /uploads/{uploadId}/parts/{partNumber}/url`, робить `PUT` безпосередньо в object storage, збирає ETag кожної частини та викликає `POST /uploads/{uploadId}/complete` із масивом `[{"partNumber": 1, "etag": "..."}, ...]`. Успішна відповідь `202 Accepted` **ще не дає права скачувати файл**: потрібно дочекатися `READY` за `GET /uploads/{uploadId}`.

#### B. Конкурентна заміна файла

```http
POST /api/v1/files/00000000-0000-4000-8000-000000000301/uploads HTTP/1.1
X-CSRF-Token: <csrf-token>
Idempotency-Key: 00000000-0000-4000-8000-000000000102
Content-Type: application/json
```

```json
{
  "expectedVersionNo": 3,
  "sizeBytes": 8192,
  "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "contentType": "application/pdf"
}
```

При `complete` сервер атомарно перевіряє `expectedVersionNo`: якщо інший користувач уже зарезервував версію `4`, другий запит отримує `409 VERSION_CONFLICT`. Успішний `complete` резервує нову версію `PENDING_SCAN`. Якщо версія `4` ще перевіряється, для скачування продовжує використовуватися попередня `CLEAN` версія `3`.

#### C. Внутрішнє посилання

`POST /files/{fileId}/share-links` з тілом:

```json
{
  "recipientUserId": "00000000-0000-4000-8000-000000000501",
  "expiresInHours": 24
}
```

Повертає `201` із `id`, `recipientUserId`, `expiresAt` і URL вигляду `https://files.example.corp/share#token=<opaque-secret>`. Одержувач повинен бути **конкретним корпоративним користувачем**, підтвердженим через SSO. SPA передає токен у JSON `POST /share-links/resolve`; відповідь містить підписаний download URL тільки для `CLEAN` версії. Повторне відкриття після revoke дає `403/404` залежно від політики приховування ресурсу.

## 8. Error Handling Design

### 8.1. Уніфікований формат помилки

Усі помилки REST API повертаються з `Content-Type: application/problem+json` за підходом **RFC 9457 (Problem Details)** із розширеннями `code`, `requestId`, `retryable`, а для валідації — `invalidParams`.

```json
{
  "type": "https://docs.example.corp/problems/version-conflict",
  "title": "Conflict",
  "status": 409,
  "detail": "Поточна версія файла відрізняється від expectedVersionNo.",
  "instance": "/api/v1/uploads/00000000-0000-4000-8000-000000000201/complete",
  "code": "VERSION_CONFLICT",
  "requestId": "c55b68c2-3f52-453b-91db-3f3b1718570a",
  "retryable": false
}
```

Поля `type` та `instance` описують тип і конкретний запит, але **не повинні містити секретні токени чи внутрішні S3 object keys**. `requestId` прив'язує відповідь до структурованих логів і аудиту. Для однакової помилки код `code` стабільний незалежно від тексту `detail`.

### 8.2. Типи помилок та поведінка

| HTTP | Код помилки (`code`) | Приклад ситуації | Реакція клієнта / бекенду |
|---|---|---|---|
| **400** Bad Request | `BAD_REQUEST`, `VALIDATION_FAILED` | Хибний UUID, некоректний JSON, відсутній `X-CSRF-Token` або обов'язкове поле | Показати поля `invalidParams`; не повторювати запит без змін; сервер логує без чутливих полів |
| **401** Unauthorized | `UNAUTHENTICATED` | Немає сесії або cookie прострочена | Переадресувати на SSO / оновити сесію, не дублювати мутацію автоматично |
| **403** Forbidden | `ACCESS_DENIED` | Роль `VIEWER` намагається завантажити нову версію; чужий share-link | Заборонити дію, записати security audit; не розкривати приватні метадані |
| **404** Not Found | `NOT_FOUND` | Ідентифікатор не існує або об'єкт приховано від користувача | Показати «Об'єкт недоступний»; не розкривати факт існування чужого ресурсу |
| **409** Conflict | `VERSION_CONFLICT`, `QUOTA_EXCEEDED`, `IDEMPOTENCY_KEY_REUSED`, `FILE_PURGING` | Другий одночасний commit, переповнена квота, повтор ключа з іншим тілом, purge | Оновити актуальний стан; запропонувати створити окрему копію; не повторювати без зміни передумов |
| **422** Unprocessable Content | `CHECKSUM_MISMATCH`, `CONTENT_REJECTED`, `QUARANTINED` | SHA-256 не збігається або файл не пройшов AV-перевірку | Файл не доступний для скачування; пояснити причину та запропонувати повторний upload / звернення до адміністратора |
| **429** Too Many Requests | `RATE_LIMITED` | Багато REST-запитів протягом хвилини | Відповідати із заголовком `Retry-After`; клієнт виконує обмежений retry з backoff та jitter |
| **500** Internal Server Error | `INTERNAL_ERROR` | Непередбачений збій | Показати `requestId`; приховати stack trace; створити алерт для повторюваних збоїв |
| **503** Service Unavailable | `DEPENDENCY_UNAVAILABLE` | Тимчасово недоступна БД або object storage | Backoff/retry **лише для безпечних або ідемпотентних операцій**, алерт і перевірка залежності |

**Приклад `400` із полями валідації:**

```json
{
  "type": "https://docs.example.corp/problems/validation-failed",
  "title": "Bad Request",
  "status": 400,
  "detail": "Не вдалося перевірити запит.",
  "code": "VALIDATION_FAILED",
  "requestId": "00000000-0000-4000-8000-000000000601",
  "retryable": false,
  "invalidParams": [
    { "name": "sizeBytes", "reason": "Максимум 2147483648 байтів" }
  ]
}
```

**Приклад `422` після перевірки файла:**

```json
{
  "type": "https://docs.example.corp/problems/content-rejected",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "Версія не пройшла перевірку безпечності й недоступна для скачування.",
  "code": "CONTENT_REJECTED",
  "requestId": "00000000-0000-4000-8000-000000000602",
  "retryable": false
}
```

**Приклад `429` із заголовком:** `Retry-After: 30`, JSON `code=RATE_LIMITED`, `retryable=true`. Клієнт не надсилає нову спробу раніше зазначеного часу.

### 8.3. Стратегія помилок за класами операцій

- **Читання метаданих (`GET`):** повторна спроба допускається для тимчасових `503` або мережевих помилок з 2–3 повтореннями та експоненційною затримкою; `403`/`404` не повторюються автоматично.
- **Мутації (`POST`):** сервер записує `Idempotency-Key` разом із хешем запиту і стабільною відповіддю в одній логічній транзакції; повтор із тим самим ключем не дублює ефект. Для `PUT`/`DELETE` метод є ідемпотентним за задумом ресурсу, але зміни прав, purge та аудит мають враховувати повторне виконання.
- **Multipart upload:** частини можна перевантажити незалежно; повторний `complete` не створює нової версії. Об'єкти, завершені в S3, але відхилені транзакцією БД через CAS, потрапляють до контрольованого cleanup.
- **Фоновий worker:** використовує retry з backoff і максимум 3 спроби, після чого переводить задачу в `FAILED` і формує алерт; не переводить неперевірену версію до `CLEAN`.
- **Доступ до S3:** якщо вже виданий signed URL повернув помилку сховища, це **не** JSON помилка FileFlow; клієнт запитує новий URL через REST API після перевірки стану і прав.
