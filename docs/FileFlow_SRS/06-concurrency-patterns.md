# 6. Concurrency Patterns Usage

[← До вступу та змісту](../FileFlow_SRS.md)

## 9. Concurrency Patterns Usage — п'ять сценаріїв

### C1. Паралельне завантаження частин одного файла

**Проблема.** Декілька частин одного файла передаються одночасно; спроби повторного `complete` або нестабільне з'єднання не повинні створювати дублікати версій.

**Патерни:** *bounded parallelism* (максимум 4 активних PUT), *idempotency key*, транзакційна фіксація метаданих.

```mermaid
sequenceDiagram
    autonumber
    actor U as Працівник
    participant SPA as Web SPA
    participant API as FileFlow API
    participant DB as PostgreSQL
    participant S3 as Object Storage
    participant W as Worker

    U->>SPA: Обрати великий файл
    SPA->>API: POST /folders/{id}/uploads
    API->>DB: Створити upload_session
    API-->>SPA: 201 uploadId, partSize, partCount
    par Частина 1
        SPA->>API: GET /uploads/{id}/parts/1/url
        API-->>SPA: signed PUT URL
        SPA->>S3: PUT part 1
        S3-->>SPA: ETag-1
    and Частина 2
        SPA->>API: GET /uploads/{id}/parts/2/url
        API-->>SPA: signed PUT URL
        SPA->>S3: PUT part 2
        S3-->>SPA: ETag-2
    end
    Note over SPA,S3: Не більше 4 PUT одночасно
    SPA->>API: POST /uploads/{id}/complete + Idempotency-Key
    API->>S3: CompleteMultipartUpload(parts)
    API->>DB: TX: reserve version, PENDING_SCAN, insert job
    API-->>SPA: 202 PROCESSING
    W->>DB: Claim scan job
    W->>S3: Read object, SHA-256 та AV
    W->>DB: Update version CLEAN або REJECTED
    SPA->>API: GET /uploads/{id}
    API-->>SPA: READY або REJECTED
```

**Механізм.** Клієнт використовує пул до чотирьох одночасних upload-операцій. Номери та ETag частин зберігаються у сесії. Завершення передбачає серверну перевірку списку частин та атомарне створення pending-версії. Якщо S3 об'єкт уже зібрано, але commit до БД не відбувся, об'єкт не стає видимим і видаляється фоновим cleanup.

**Гарантія / тест:** 20 паралельних сесій з повторними `complete` не повинні створювати більше однієї версії **на одну сесію**.

### C2. Двоє працівників одночасно оновлюють той самий файл

**Проблема.** Працівники A і B бачили версію `3`, обидва завантажили свої зміни. Без синхронізації другий запис міг би непомітно перезаписати перший (*lost update*).

**Патерн:** *optimistic concurrency control* через `expectedVersionNo` та атомарну операцію **compare-and-swap (CAS)** у PostgreSQL.

```mermaid
sequenceDiagram
    autonumber
    actor A as Працівник A
    actor B as Працівник B
    participant API as FileFlow API
    participant DB as PostgreSQL

    Note over A,B: Обидва бачать latestVersionNo = 3
    par Завантаження A
        A->>API: Upload session (expectedVersionNo=3)
        API-->>A: uploadId A
    and Завантаження B
        B->>API: Upload session (expectedVersionNo=3)
        API-->>B: uploadId B
    end
    A->>API: POST complete (session A)
    API->>DB: CAS latestVersionNo 3 → 4 у TX
    DB-->>API: Updated 1 row
    API-->>A: 202 version 4 PENDING_SCAN
    B->>API: POST complete (session B)
    API->>DB: CAS latestVersionNo 3 → 4 у TX
    DB-->>API: Updated 0 rows
    API-->>B: 409 VERSION_CONFLICT, current=4
    Note over B,API: Клієнт може зберегти зміни як окремий файл
```

**Механізм.** Унікальний `(file_id, version_no)` і умова `WHERE latest_version_no = expectedVersionNo` забезпечують атомарність. Відхилена версія B не робиться поточною, однак локальні зміни B залишаються в клієнта; інтерфейс пропонує завантажити їх окремим файлом. Операція restore попередньої версії використовує ту саму перевірку.

**Гарантія / тест:** із двох конкурентних завершень при `expectedVersionNo=3` рівно одне резервує `4`, інше отримує `409`, і початкова версія `3` залишається незмінною.

### C3. Одночасне відкликання прав і спроба скачування

**Проблема.** Керівник відкликає доступ саме в момент, коли працівник запитує посилання на скачування. Не можна видавати *нові* посилання після commit відкликання прав.

**Патерни:** *transactional authorization check*, коротка критична секція з row-level locks для стану ACL, **short-lived capability** (signed URL).

```mermaid
sequenceDiagram
    autonumber
    actor E as Працівник
    actor O as Керівник
    participant API as FileFlow API
    participant DB as PostgreSQL
    participant S3 as Object Storage

    par Запит на скачування
        E->>API: GET /files/{id}/download-link
        API->>DB: BEGIN + lock ACL revision FOR SHARE
        DB-->>API: Permission granted
        API-->>E: signed GET URL (TTL 60 s)
        API->>DB: COMMIT
    and Відкликання прав
        O->>API: DELETE /files/{id}/permissions/{userId}
        API->>DB: TX update ACL revision / revoke
        Note over API,DB: UPDATE чекає, якщо триває lock читання
        DB-->>API: COMMIT, permission revoked
        API-->>O: 204 No Content
    end
    E->>API: Новий GET /download-link після revoke
    API->>DB: Check effective ACL
    DB-->>API: Denied
    API-->>E: 403 ACCESS_DENIED
    Note over E,S3: URL, виданий ДО revoke, ще може діяти ≤60 с
```

**Механізм.** Перевірка ACL та видача signed URL виконуються в короткій транзакційно впорядкованій секції; відкликання змінює ревізію прав під ексклюзивним row lock. Прямий доступ до S3 **неможливо відкликати миттєво**, якщо URL уже виданий. Працівник може мати інший успадкований доступ — у такому разі видалення персонального правила саме по собі не забороняє читання.

**Гарантія / тест:** після commit відкликання, за відсутності інших грантів, новий REST-запит повертає `403`; наявні URL втрачають чинність не пізніше 60 с.

### C4. Конкурентні worker-и перевіряють файли

**Проблема.** Два worker-процеси одночасно опитують одну чергу PostgreSQL; не можна двічі виконати ефект обробки того самого job, а збій worker не повинен втратити завдання.

**Патерни:** *competing consumers*, `SELECT ... FOR UPDATE SKIP LOCKED`, *job lease*, ідемпотентний запис результатів, *bounded retries*.

```mermaid
sequenceDiagram
    autonumber
    participant W1 as Worker 1
    participant W2 as Worker 2
    participant DB as PostgreSQL jobs
    participant S3 as Object Storage

    par Worker 1 claims job
        W1->>DB: TX SELECT job FOR UPDATE SKIP LOCKED
        DB-->>W1: job A
        W1->>DB: Mark A RUNNING + lease, COMMIT
    and Worker 2 claims job
        W2->>DB: TX SELECT job FOR UPDATE SKIP LOCKED
        DB-->>W2: job B (A skipped)
        W2->>DB: Mark B RUNNING + lease, COMMIT
    end
    W1->>S3: Read object A, verify hash and AV
    W2->>S3: Read object B, verify hash and AV
    W1->>DB: TX conditional update A, job DONE, audit
    W2->>DB: TX conditional update B, job DONE, audit
    Note over W1,DB: Expired lease → retry#59; result update idempotent
```

**Механізм.** Claim транзакція швидко позначає роботу як `RUNNING` і встановлює `lease_until`, після чого звільняє блокування. Перевірка виконується поза транзакцією. Якщо worker завершується аварійно, інший може повторно забрати завдання після lease timeout. Запис `CLEAN`/`REJECTED` виконується лише за очікуваного стану, а audit/job completion — ідемпотентно в одній транзакції. Після трьох невдалих спроб job переходить у `FAILED` і формує алерт — жодне `PENDING_SCAN` не стає `CLEAN` за замовчуванням.

**Гарантія / тест:** один `jobId` може бути опрацьований повторно, але кінцевий стан версії та подія аудиту фіксуються **без дублювання**.

### C5. Фонове фізичне видалення проти відновлення файла

**Проблема.** Worker починає остаточне видалення після retention, а працівник майже одночасно натискає «Відновити». Не можна відновити файл, об'єкт якого вже видалений зі сховища.

**Патерни:** *state machine / conditional transition*, **CAS**, *soft-delete + delayed purge*, *idempotent cleanup*; перевірка legal hold.

```mermaid
sequenceDiagram
    autonumber
    actor U as Працівник
    participant API as FileFlow API
    participant W as Purge worker
    participant DB as PostgreSQL
    participant S3 as Object Storage

    par Запит відновлення
        U->>API: POST /files/{id}/recover
        API->>DB: TX: TRASHED → ACTIVE if not PURGING
    and Фонове очищення
        W->>DB: TX: TRASHED → PURGING if purgeAt passed and !hold
    end
    alt Recover wins CAS
        DB-->>API: ACTIVE
        API-->>U: 200 restored
        DB-->>W: CAS failed — do not purge
    else Purge wins CAS
        DB-->>W: PURGING
        DB-->>API: File state not TRASHED
        API-->>U: 409 FILE_PURGING
        W->>S3: Delete unreferenced objects (idempotent)
        W->>DB: Set PURGED + audit
    else Legal hold present
        DB-->>W: Purge blocked
        W->>DB: Reschedule job
    end
```

**Механізм.** Відновлення і початок purge змагаються за один умовний перехід із `TRASHED`. Тільки переможець може змінити стан. Worker перевіряє `legal_hold=false`, `purge_at <= now()` та відсутність інших посилань на S3-об'єкт. Операція S3 delete повторювана: якщо видалення перерветься, job продовжиться; `PURGED` записується тільки після підтвердженого завершення.

**Гарантія / тест:** ніколи не існує `ACTIVE` файла, всі фізичні версії якого вже видалені; якщо recovery виграв перехід, purge не виконується.

### 9.6. Зведення concurrency-рішень

| Flow | Сценарій | Патерн | Яку властивість забезпечує |
|---|---|---|---|
| **C1** | Multipart upload | Bounded parallelism, idempotency | Повторне завершення не породжує зайві версії |
| **C2** | Конкурентне оновлення версії | Optimistic concurrency, CAS | Немає lost update; конфлікт виявляється через `409` |
| **C3** | Скачування vs ACL revoke | Transactional authorization, short-lived capability | Немає нових signed URL після відкликання; старі обмежені TTL |
| **C4** | Паралельне сканування | Competing consumers, SKIP LOCKED, lease | Jobs не губляться; повторна обробка не змінює результат двічі |
| **C5** | Purge vs recovery | CAS state transition, delayed cleanup | Файл не можна відновити після початку незворотного видалення |
