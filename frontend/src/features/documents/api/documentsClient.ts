import { apiClient } from "@/api/client";
import { createDocumentsApi } from "./documentsApi";

export const documentsApi = createDocumentsApi(apiClient);
