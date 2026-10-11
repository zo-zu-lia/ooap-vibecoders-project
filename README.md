# FileFlow

Навчальний проєкт корпоративної системи зберігання та поширення файлів для однієї компанії — аналог Dropbox із корпоративним входом, робочими просторами, версіями файлів і контролем доступу.

Репозиторій містить локальну Docker-реалізацію, тести, проєктну документацію та OpenAPI-контракт.

## Локальний запуск

```sh
./scripts/start.sh
```

Застосунок: **http://localhost:8080**. Згенеровані облікові дані: `.local/credentials.txt`.
Docker Compose запускає API, worker, PostgreSQL, приватне S3-сховище, ClamAV, Dex OIDC та reverse proxy з лімітами пам'яті.

[Local development guide](./docs/LOCAL_DEVELOPMENT.md) описує запуск, ролі, тести, резервне копіювання, відновлення й моніторинг.

## Документація

Почніть із [загального вступу та змісту специфікації](./docs/FileFlow_SRS.md). Там також наведено критерії верифікації, перелік змін і джерела.

| Пункт завдання | Документ | Зміст |
|---|---|---|
| 3 | [Опис продукту](./docs/FileFlow_SRS/03-product-description.md) | Актори, сценарії використання, функціональні й нефункціональні вимоги, Identity Management, модель даних та UML |
| 4 | [Архітектура](./docs/FileFlow_SRS/04-architecture.md) | SPA, модульний моноліт, фонові worker-и, PostgreSQL, S3-сумісне сховище та життєвий цикл файла |
| 5 | [Error Handling Design](./docs/FileFlow_SRS/05-error-handling.md) | REST API, контракти відповідей, коди помилок та стратегії обробки |
| 6 | [Concurrency Patterns Usage](./docs/FileFlow_SRS/06-concurrency-patterns.md) | П’ять сценаріїв конкурентного виконання, діаграми послідовності та гарантії коректності |

[OpenAPI-контракт](./docs/FileFlow_OpenAPI.yaml) описує 29 REST-операцій у форматі OpenAPI 3.0.3.

## Додаткові моделі

Другий етап завдання — до **10.12.2026**. Документи деталізують проєктні рішення. Поточна локальна реалізація та межі перевірених гарантій описані в [Local development guide](./docs/LOCAL_DEVELOPMENT.md); вихідні SRS-моделі збережено як проєктну специфікацію.

| Документ | Зміст |
|---|---|
| [Data model](./docs/FileFlow_SRS/07-data-model.md) | 16 основних сутностей, ER-діаграми, класифікація й retention, consensus, batching, streaming, consistency |
| [Resiliency model](./docs/FileFlow_SRS/08-resiliency-model.md) | CID із 5 взаємодіями, RMA workbook із 15 сценаріями відмов, recovery та fault injection |
| [Security model](./docs/FileFlow_SRS/09-security-model.md) | 2 потоки з межами довіри, STRIDE, 10 загроз із mitigation plan |
| [Deployment model](./docs/FileFlow_SRS/10-deployment-model.md) | Інфраструктурна діаграма однієї dev-VM, ресурси, мережа, release/rollback і backup |
| [Analytics model](./docs/FileFlow_SRS/11-analytics-model.md) | 12 функціональних метрик із FR-зв'язками та 2 воронки |
| [Monitoring & alerting model](./docs/FileFlow_SRS/12-monitoring-alerting-model.md) | 15 операційних метрик, 10 alert rules, пороги, відповідальні й mitigation |
