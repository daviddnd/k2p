# Frontend image: static build served by nginx (ADR-0001 separate React frontend).
FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.29-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
# TLS variant (R8): both configs ship in the image; the compose command swaps
# this in as default.conf only when a cert directory is mounted.
COPY deploy/nginx.tls.conf /etc/nginx/tls.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
EXPOSE 443
