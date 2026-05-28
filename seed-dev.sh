#!/bin/bash
set -e

COMPOSE_FILE="/home/nananina/docker-compose.dev.yaml"
ENV_FILE="/home/nananina/.env"

MYSQL_ROOT_PASSWORD=$(grep '^MYSQL_ROOT_PASSWORD=' "$ENV_FILE" | cut -d= -f2)

exec_sql() {
  docker compose -f "$COMPOSE_FILE" exec -T mysql \
    mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --default-character-set=utf8mb4 sight -e "$1"
}

echo "=== 회원 시딩 ==="
exec_sql "
INSERT INTO khlug_members
  (id, name, number, admission, realname, college, grade, state, expoint, active, manager, khuisauth_at, updated_at, created_at, last_login, last_enter)
VALUES
  (1, 'member',  20240001, '24', '일반회원', '정보대학', 2, 1, 0, 1, 0, NOW(), NOW(), NOW(), NOW(), NOW()),
  (2, 'manager', 20240002, '24', '관리자',   '정보대학', 4, 1, 0, 1, 1, NOW(), NOW(), NOW(), NOW(), NOW())
ON DUPLICATE KEY UPDATE manager = VALUES(manager);
"

echo "=== 일정 시딩 ==="
exec_sql "
INSERT INTO schedule
  (id, category, title, author, state, scheduled_at, end_at, location, expoint, check_code, created_at, updated_at)
VALUES
  (1, 'SEMINAR',      '개발 세미나 (테스트)',   2, 'public', NOW() - INTERVAL 30 MINUTE, NOW() + INTERVAL 90 MINUTE, '정보관 401호', 0, NULL, NOW(), NOW()),
  (2, 'CLUB',         '정기 모임 (테스트)',      2, 'public', NOW() + INTERVAL 2 HOUR,   NOW() + INTERVAL 4 HOUR,    '정보관 401호', 0, NULL, NOW(), NOW())
ON DUPLICATE KEY UPDATE title = VALUES(title);
"

echo "=== 완료 ==="
