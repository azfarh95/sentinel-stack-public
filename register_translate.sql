BEGIN;

INSERT INTO mcp_servers (uuid, name, type, url)
VALUES ('00000000-0000-0000-0000-000000008094', 'translate-mcp', 'STREAMABLE_HTTP', 'http://translate-mcp:8094/mcp')
ON CONFLICT ON CONSTRAINT mcp_servers_name_user_unique_idx DO UPDATE
  SET type = EXCLUDED.type, url = EXCLUDED.url;

INSERT INTO namespace_server_mappings (namespace_uuid, mcp_server_uuid, status)
SELECT '0a83b85b-24ea-4491-b24b-17104bc9bba0',
       (SELECT uuid FROM mcp_servers WHERE name = 'translate-mcp' AND user_id IS NULL),
       'ACTIVE'
ON CONFLICT ON CONSTRAINT namespace_server_mappings_unique_idx DO NOTHING;

COMMIT;

SELECT s.uuid, s.name, s.type, s.url, m.status
FROM mcp_servers s
LEFT JOIN namespace_server_mappings m
  ON m.mcp_server_uuid = s.uuid
 AND m.namespace_uuid  = '0a83b85b-24ea-4491-b24b-17104bc9bba0'
WHERE s.name = 'translate-mcp';
