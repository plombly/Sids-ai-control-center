# The site: WordPress with this project's theme and content built in. LNMT builds this
# on every push and keeps the WordPress files, uploads and database in their
# own volumes, so a new theme never touches the site's content.
FROM docker.io/library/wordpress:php8.3-apache
COPY theme/ /opt/site-theme/
COPY mu-plugins/ /opt/site-mu-plugins/
COPY content/ /opt/site-content/
COPY lnmt-entrypoint.sh /usr/local/bin/lnmt-entrypoint.sh
RUN chmod 755 /usr/local/bin/lnmt-entrypoint.sh
ENTRYPOINT ["lnmt-entrypoint.sh"]
CMD ["apache2-foreground"]
