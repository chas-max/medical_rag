import { request } from "./client";

export interface ChunkDetail {
  chunk_id: string;
  document_id: string;
  source_id: string;
  filename: string;
  section: string;
  page: number | null;
  ordinal: number;
  text: string;
  parent_text: string;
  parent_ordinal: number;
  token_count: number;
}

/** Fetch one stored knowledge-base block for the citation source panel. */
export const getChunk = (chunkId: string) =>
  request<ChunkDetail>(`/chunks/${encodeURIComponent(chunkId)}`);
