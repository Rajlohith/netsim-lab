import React, { createContext, useContext } from "react";
import { api as defaultApi } from "../api/endpoints.js";

// Pages get the API object from context so tests (and other deployments) can inject their own.
const ApiContext = createContext(defaultApi);
export const ApiProvider = ({ api, children }) => <ApiContext.Provider value={api}>{children}</ApiContext.Provider>;
export const useApi = () => useContext(ApiContext);
