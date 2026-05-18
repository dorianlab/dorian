FROM mongo:latest

HEALTHCHECK --interval=30s --timeout=5s \
  CMD mongosh --eval "db.adminCommand('ping')"

EXPOSE 27017