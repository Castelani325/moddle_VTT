FROM php:8.3-apache

# System dependencies
RUN apt-get update && apt-get install -y \
    curl gnupg2 unzip git \
    libicu-dev libpng-dev libjpeg-dev libfreetype6-dev \
    libxml2-dev libzip-dev libonig-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

# Microsoft ODBC Driver 18 for SQL Server (Debian 12 Bookworm)
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

# PHP extensions required by Moodle
RUN docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install \
        gd intl soap xml xsl zip mbstring exif opcache

# SQL Server PHP extensions
RUN pecl install sqlsrv pdo_sqlsrv \
    && docker-php-ext-enable sqlsrv pdo_sqlsrv

# Composer
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer

# Apache modules
RUN a2enmod rewrite headers

# Custom PHP config and Apache vhost
COPY .docker/php/custom.ini /usr/local/etc/php/conf.d/moodle.ini
COPY .docker/apache/moodle.conf /etc/apache2/sites-available/000-default.conf

WORKDIR /var/www/html

# Copy source and install PHP dependencies
COPY . .
RUN composer install --no-dev --optimize-autoloader --no-interaction

# Moodle data directory (outside webroot)
RUN mkdir -p /var/moodledata \
    && chown -R www-data:www-data /var/www/html /var/moodledata \
    && chmod -R 755 /var/www/html \
    && chmod -R 770 /var/moodledata

COPY .docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
CMD ["apache2-foreground"]
