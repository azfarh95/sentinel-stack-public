DO $$
DECLARE
  server_uuid  uuid := '5d91419b-8d84-49ce-a2fe-6d611fea61ca';
  ns_uuid      uuid := '0a83b85b-24ea-4491-b24b-17104bc9bba0';
  t_uuid       uuid;

  tools jsonb := '[
    {"name":"video_info",      "desc":"Get metadata for a video URL without downloading it. Returns title, uploader, duration, upload date, view count, thumbnail URL, and description snippet."},
    {"name":"list_formats",    "desc":"List all available quality and format combinations for a video URL. Shows format_id, resolution, fps, codec, and estimated file size."},
    {"name":"download_video",  "desc":"Start downloading a video. Returns a job_id immediately - the download runs in the background. Use check_download(job_id) to track progress."},
    {"name":"check_download",  "desc":"Check the status of a download job. Status: queued | downloading | done | error. When done: shows filename and file path."},
    {"name":"list_downloads",  "desc":"List recent download jobs, most recent first."},
    {"name":"send_to_telegram","desc":"Send a downloaded file to a Telegram chat via the bot. Takes chat_id and filepath from check_download result."}
  ]';
  tool jsonb;

BEGIN
  FOR tool IN SELECT * FROM jsonb_array_elements(tools)
  LOOP
    INSERT INTO tools (mcp_server_uuid, name, description, tool_schema)
    VALUES (
      server_uuid,
      tool->>'name',
      tool->>'desc',
      '{}'::jsonb
    )
    ON CONFLICT DO NOTHING
    RETURNING uuid INTO t_uuid;

    IF t_uuid IS NULL THEN
      SELECT uuid INTO t_uuid FROM tools WHERE mcp_server_uuid = server_uuid AND name = tool->>'name';
    END IF;

    INSERT INTO namespace_tool_mappings (namespace_uuid, tool_uuid, mcp_server_uuid, status)
    VALUES (ns_uuid, t_uuid, server_uuid, 'ACTIVE')
    ON CONFLICT DO NOTHING;

    RAISE NOTICE 'Mapped tool: %', tool->>'name';
  END LOOP;
END $$;
