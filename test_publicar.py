import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from publicar import ErrorSeguro, Instagram, ejecutar, publicado_hoy


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

    def test_pieza_fechada_no_se_publica_antes_de_su_hora_ni_fuera_del_dia(self):
        self.posts[0]['publish_date'] = '2026-11-16'
        (self.root / 'calendario.json').write_text(json.dumps(self.posts), encoding='utf-8')
        for day, hour, minute in [(15, 14, 0), (16, 12, 59), (17, 1, 0), (17, 13, 0)]:
            with self.subTest(day=day, hour=hour, minute=minute):
                now = datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)
                ejecutar('publicar', self.root, self.api, Mock(), self.images, now=now)
        self.api.comprobar_cuenta.assert_not_called()
        self.api.solicitar.assert_not_called()

    def test_fecha_vencida_no_desplaza_la_pieza_del_dia(self):
        self.posts[0]['publish_date'] = '2026-11-16'
        self.posts.append({**self.posts[0], 'publish_date': '2026-11-17', 'caption': 'Segunda pieza'})
        (self.root / 'calendario.json').write_text(json.dumps(self.posts), encoding='utf-8')
        self.api.solicitar.side_effect = [{'id': 'c1'}, {'status_code': 'FINISHED'}, {'id': 'm1'}]
        persist = Mock()
        now = datetime(2026, 11, 17, 20, tzinfo=timezone.utc)
        ejecutar('publicar', self.root, self.api, persist, self.images, now=now)
        self.images.assert_called_once_with('', 1)
        self.assertEqual(self.api.solicitar.call_args_list[0].args[2]['caption'], 'Segunda pieza')
        self.assertEqual(persist.call_args.args[0]['published'][0]['item'], 2)

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

    def test_segundo_disparo_del_dia_no_envia_siguiente_pieza(self):
        self.state['published'] = [{'item': 0, 'media_id': 'previo', 'published_at': datetime.now(timezone.utc).isoformat()}]
        self.guardar(self.state)
        persist = Mock()
        ejecutar('publicar', self.root, self.api, persist, self.images)
        self.api.comprobar_cuenta.assert_not_called()
        self.api.solicitar.assert_not_called()
        persist.assert_not_called()
        self.assertEqual(json.loads((self.root / 'estado.json').read_text())['next_index'], 0)

    def test_dia_chileno_no_se_confunde_con_medianoche_utc(self):
        state = {'published': [{'published_at': '2026-11-03T01:30:00+00:00'}]}
        self.assertTrue(publicado_hoy(state, datetime(2026, 11, 2, 23, tzinfo=timezone.utc)))
        self.assertFalse(publicado_hoy(state, datetime(2026, 11, 3, 13, tzinfo=timezone.utc)))

    def test_fecha_invalida_bloquea_para_no_duplicar(self):
        with self.assertRaisesRegex(ErrorSeguro, 'fecha verificable'):
            publicado_hoy({'published': [{'published_at': 'incorrecta'}]})


if __name__ == '__main__':
    unittest.main()
