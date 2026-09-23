FROM redis:7-alpine

COPY cloudflared /usr/local/bin/cloudflared
RUN chmod +x /usr/local/bin/cloudflared

ENTRYPOINT ["/usr/local/bin/cloudflared"]
