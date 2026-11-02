import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from publicar import ErrorSeguro, Instagram, ejecutar


class PublicadorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.posts = [{'title': 'Prueba', 'caption': 'Contenido preventivo.'}]
        (self.root / 'calendario.json').write_text(json.dumps(self.posts), encoding='utf-8')
        self.state = {'next_index': 0, 'published': []}
        self.guardar(self.state)
        self.api = Mock(uid='123')
        self.images = Mock(return_value='https://example.org/posts/01.jpg')

    def tearDown(self):
        self.tmp.cleanup()

    def guardar(self, state):
        (self.root / 'estado.json').write_text(json.dumps(state), encoding='utf-8')

    def test_validar_no_crea_ni_publica(self):
        persist = Mock()
        ejecutar('validar', self.root, self.api, persist, self.images)
        self.api.comprobar_cuenta.assert_called_once()
        self.api.solicitar.assert_not_called()
        persist.assert_not_called()

    def test_envio_incierto_bloquea_antes_de_contactar_meta(self):
        self.state['pending'] = {'index': 0, 'phase': 'publishing', 'container_id': 'c1'}
        self.guardar(self.state)
        with self.assertRaisesRegex(ErrorSeguro, 'conciliación'):
            ejecutar('publicar', self.root, self.api, Mock(), self.images)
        self.api.comprobar_cuenta.assert_not_called()
        self.api.solicitar.assert_not_called()

    def test_estado_persistido_antes_del_unico_envio(self):
        phases = []
        def persist(state, root):
            phases.append(copy.deepcopy(state))
            self.guardar(state)
        def request(method, path, data):
            if path == '123/media':
                return {'id': 'c1'}
            if path == 'c1':
                return {'status_code': 'FINISHED'}
            if path == '123/media_publish':
                self.assertEqual(phases[-1]['pending']['phase'], 'publishing')
                return {'id': 'm1'}
            raise AssertionError('Ruta inesperada')
        self.api.solicitar.side_effect = request
        ejecutar('publicar', self.root, self.api, persist, self.images)
        self.assertEqual([s.get('pending', {}).get('phase') for s in phases], ['prepared', 'publishing', None])
        self.assertEqual(phases[-1]['next_index'], 1)
        self.assertEqual(phases[-1]['published'][0]['media_id'], 'm1')
        self.assertEqual(sum(c.args[1] == '123/media_publish' for c in self.api.solicitar.call_args_list), 1)

    def test_timeout_al_enviar_no_permite_repetir(self):
        self.api.solicitar.side_effect = [{'id': 'c1'}, {'status_code': 'FINISHED'}, ErrorSeguro('Respuesta incierta')]
        def persist(state, root):
            self.guardar(state)
        with self.assertRaises(ErrorSeguro):
            ejecutar('publicar', self.root, self.api, persist, self.images)
        state = json.loads((self.root / 'estado.json').read_text())
        self.assertEqual(state['pending']['phase'], 'publishing')
        self.api.reset_mock()
        with self.assertRaisesRegex(ErrorSeguro, 'conciliación'):
            ejecutar('publicar', self.root, self.api, persist, self.images)
        self.api.solicitar.assert_not_called()

    def test_cuenta_incorrecta_no_se_acepta(self):
        session = Mock()
        session.request.return_value.ok = True
        session.request.return_value.json.return_value = {'id': '123', 'username': 'otra_cuenta'}
        api = Instagram('valor-privado-de-prueba', '123', 'eicvconsultores', session=session)
        with self.assertRaisesRegex(ErrorSeguro, 'perfil esperado'):
            api.comprobar_cuenta()
        self.assertEqual(session.request.call_count, 1)

    def test_error_no_incluye_respuesta_privada(self):
        session = Mock()
        session.request.return_value.ok = False
        session.request.return_value.status_code = 400
        session.request.return_value.json.return_value = {'error': {'code': 190, 'message': 'datos privados de prueba'}}
        api = Instagram('valor-privado-de-prueba', '123', 'eicvconsultores', session=session)
        api.base = 'https://graph.instagram.com'
        with self.assertRaises(ErrorSeguro) as raised:
            api.solicitar('GET', '123')
        self.assertNotIn('datos privados', str(raised.exception))
        self.assertNotIn('valor-privado', str(raised.exception))

    def test_calendario_terminado_no_se_repite(self):
        self.guardar({'next_index': 1, 'published': [{'item': 1, 'media_id': 'm1'}]})
        ejecutar('publicar', self.root, self.api, Mock(), self.images)
        self.api.comprobar_cuenta.assert_not_called()


if __name__ == '__main__':
    unittest.main()
