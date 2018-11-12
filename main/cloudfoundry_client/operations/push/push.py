import os
import shutil
import tempfile
import time

from cloudfoundry_client.imported import urlparse
from cloudfoundry_client.operations.push.file_helper import FileHelper
from cloudfoundry_client.operations.push.validation.manifest import ManifestReader


class PushOperation(object):
    def __init__(self, client):
        self.client = client

    def push(self, space_id, manifest_path):
        app_manifests = ManifestReader.load_application_manifests(manifest_path)
        organization, space = self._retrieve_space_and_organization(space_id)

        for app_manifest in app_manifests:
            if 'path' in app_manifest:
                self._push_application(organization, space, app_manifest)
            elif 'docker' in app_manifest:
                self._push_docker(organization, space, app_manifest)

    def _retrieve_space_and_organization(self, space_id):
        space = self.client.v2.spaces.get(space_id)
        organization = space.organization()
        return organization, space

    def _push_application(self, organization, space, app_manifest):
        app = self._init_application(space, app_manifest)
        self._route_application(organization, space, app, app_manifest.get('no-route', False),
                                app_manifest.get('routes', []), app_manifest.get('random-route', False))
        self._upload_application(app, app_manifest['path'])
        self._bind_services(space, app, app_manifest.get('services', []))
        PushOperation._restart_application(app)

    def _push_docker(self, organization, space, app_manifest):
        app = self._init_application(space, app_manifest)
        self._route_application(organization, space, app, app_manifest.get('no-route', False),
                                app_manifest.get('routes', []), app_manifest.get('random-route', False))
        self._bind_services(space, app, app_manifest.get('services', []))
        PushOperation._restart_application(app)

    def _init_application(self, space, app_manifest):
        app = self.client.v2.apps.get_first(name=app_manifest['name'], space_guid=space.metadata.guid)
        return self._update_application(app, app_manifest) if app is not None \
            else self._create_application(space, app_manifest)

    def _create_application(self, space, app_manifest):
        request = self._build_request_from_manifest(app_manifest)
        request['space_guid'] = space['metadata']['guid']
        if request.get('health-check-type') == 'http' and request.get('health-check-http-endpoint') is None:
            request['health-check-http-endpoint'] = '/'
        return self.client.v2.apps.create(request)

    def _update_application(self, app_manifest, app):
        request = self._build_request_from_manifest(app_manifest)
        request['environment_json'] = PushOperation._merge_environment(app, app_manifest)
        if request.get('health-check-type') == 'http' and request.get('health-check-http-endpoint') is None \
                and app['entity'].get('health_check_http_endpoint') is None:
            request['health-check-http-endpoint'] = '/'
        return self.client.v2.apps.update(app['metadata']['guid'], **request)

    def _build_request_from_manifest(self, app_manifest):
        request = dict()
        request.update(app_manifest)
        stack = self.client.v2.stacks.get_first(name=app_manifest['stack']) if 'stack' in app_manifest else None
        if stack is not None:
            request['stack_guid'] = stack['metadata']['guid']
        docker = request.pop('docker', None)
        if docker is not None and 'image' in docker:
            request['docker_image'] = docker['image']
            request['diego'] = True
            if 'username' in docker and 'password' in docker:
                request['docker_credentials'] = dict(username=docker['username'], password=docker['password'])
        return request

    @staticmethod
    def _merge_environment(app, app_manifest):
        environment = dict()
        if 'environment_json' in app['entity']:
            environment.update(app['entity']['environment_json'])
        if 'env' in app_manifest:
            environment.update(app_manifest['env'])
        return environment

    def _route_application(self, organization, space, app, no_route, routes, random_route):
        existing_routes = [route for route in app.routes()]
        if no_route:
            self._remove_all_routes(app, existing_routes)
        elif len(routes) == 0 and len(existing_routes) == 0:
            self._build_default_route(space, app, random_route)
        else:
            self._build_new_requested_routes(organization, space, app, existing_routes, routes)

    def _remove_all_routes(self, app, routes):
        for route in routes:
            self.client.v2.apps.remove_route(app['metadata']['guid'], route['metadata']['guid'])

    def _build_default_route(self, space, app, random_route):
        shared_domain = None
        for domain in self.client.v2.shared_domains.list():
            if not domain['entity']['internal']:
                shared_domain = domain
                break
        if shared_domain is None:
            raise AssertionError('No route specified and no no-route field or shared domain')
        if shared_domain['entity'].get('router_group_type') == 'tcp':
            route = self.client.v2.routes.create_tcp_route(shared_domain['metadata']['guid'],
                                                        space['metadata']['guid'])
        elif random_route:
            route = self.client.v2.routes.create_host_route(shared_domain['metadata']['guid'],
                                                         space['metadata']['guid'],
                                                         '%s-%d' % (app['entity']['name'], int(time.time())))
        else:
            route = self.client.v2.routes.create_host_route(shared_domain['metadata']['guid'],
                                                         space['metadata']['guid'],
                                                         app['entity']['name'])
        self.client.v2.apps.associate_route(app['metadata']['guid'], route['metadata']['guid'])

    def _build_new_requested_routes(self, organization, space, app, existing_routes, requested_routes):
        private_domains = {domain['entity']['name']: domain for domain in organization.private_domains.list()}
        shared_domains = {domain['entity']['name']: domain for domain in organization.shared_domains.list()}
        for requested_route in requested_routes:
            route, port, path = PushOperation._split_route(requested_route)
            if len(path) > 0 and port is not None:
                raise AssertionError('Cannot set both port and path for route: %s' % requested_route)
            host, domain_name, domain = PushOperation._resolve_domain(route, private_domains, shared_domains)
            if port is not None and host is not None:
                raise AssertionError(
                    'For route (%s) refers to domain %s that is a tcp one. It is hence routed by port and not by host'
                    % (requested_route, domain_name))
            route_created = None
            if port is not None and domain['entity'].get('router_group_type') != 'tcp':
                raise AssertionError('Cannot set port on route(%s) for non tcp domain' % requested_route)
            elif domain['entity'].get('router_group_type') == 'tcp' and port is None:
                raise AssertionError('Please specify a port on route (%s) for tcp domain' % requested_route)
            elif domain['entity'].get('router_group_type') == 'tcp':
                if not any([route['entity']['domain_guid'] == domain['metadata']['guid']
                            and route['entity']['port'] == port] for route in existing_routes):
                    route_created = self.client.v2.routes.create_tcp_route(domain['metadata']['guid'],
                                                                        space['metadata']['guid'],
                                                                        port)
            else:
                if not any([route['entity']['domain_guid'] == domain['metadata']['guid']
                            and route['entity']['host'] == host] for route in existing_routes):
                    route_created = self.client.v2.routes.create_host_route(domain_name['metadata']['guid'],
                                                                         space['metadata']['guid'],
                                                                         host,
                                                                         path)
            if route_created is not None:
                self.client.v2.apps.associate_route(app['metadata']['guid'], route_created['metadata']['guid'])

    @staticmethod
    def _split_route(requested_route):
        route_parsed = urlparse(requested_route['route'])
        idx = route_parsed.netloc.find(':')
        if 0 < idx < (len(route_parsed.netloc) - 2):
            domain = route_parsed.netloc[:idx]
            port = int(route_parsed.netloc[idx + 1:])
        elif idx >= 0:
            raise AssertionError('Invalid route format: %s' % requested_route)
        else:
            domain = route_parsed.netloc
            port = None
        return domain, port, '' if route_parsed.path == '/' else route_parsed.path

    @staticmethod
    def _resolve_domain(route, private_domains, shared_domains):
        for domains in [private_domains, shared_domains]:
            if route in domains:
                return '', route, domains[route]
            else:
                idx = route.find('.')
                if 0 < idx < (len(route) - 2):
                    host = route[:idx]
                    domain = route[idx + 1:]
                    if domain in domains:
                        return host, domain, domains[domain]
        raise AssertionError('Cannot find domain for route %s' % route)

    def _upload_application(self, app, path):
        if os.path.isfile(path):
            tmp_dir = tempfile.mkdtemp()
            try:
                FileHelper.unzip(path, tmp_dir)
                self._upload_application(app, tmp_dir)
            finally:
                shutil.rmtree(tmp_dir)
        elif os.path.isdir(path):
            _, temp_file = tempfile.mkstemp()
            try:
                application_items = {}
                for directory, file_names in FileHelper.walk(path):
                    for file_name in file_names:
                        file_location = os.path.join(path, directory, file_name)
                        application_items[os.path.join(directory, file_name)] = dict(
                            sha1=FileHelper.sha1(file_location),
                            size=FileHelper.size(file_location),
                            mode=FileHelper.mode(file_location))
                already_uploaded_items = self._already_uploaded([dict(sha1=item["sha1"], size=item["size"])
                                                                 for item in application_items.values()])
                already_uploaded_items = ['%s-%d' % (item["sha1"], item["size"]) for item in already_uploaded_items]

                def _not_already_uploaded(item):
                    description = application_items[item]
                    return '%s-%d' % (description["sha1"], description["size"]) not in already_uploaded_items

                FileHelper.zip(temp_file, path, _not_already_uploaded)
                self._upload(temp_file)
            finally:
                os.remove(temp_file)
        else:
            raise AssertionError('Path %s is neither a directory nor a file' % path)

    def _bind_services(self, space, app, services):
        for service_name in services:
            service_instance = next(space.service_instances(name=service_name), None)
            if service_instance is None:
                raise AssertionError('No service found with name %s' % service_instance)
            self.client.v2.service_bindings.create(app['metadata']['guid'], service_instance['metadata']['guid'])

    @staticmethod
    def _restart_application(app):
        app.stop()
        app.start()

    def _already_uploaded(self, items):
        return self.client.put('%s/v2/resource_match' % self.client.info.api_endpoint, json=items)

    def _upload(self, tmpname):
        raise NotImplementedError('To be implemented')
