# Build stage.
FROM node:20-alpine AS build
RUN npm ci

FROM alpine:3.19 AS runtime
COPY --from=build /app /app
